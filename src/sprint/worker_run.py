"""Root-owned Linux job launcher; files persist between jobs, processes do not."""

import ctypes
import json
import os
from pathlib import Path
import pwd
import re
import signal
import subprocess
import sys
import tempfile
import time

OUTPUT_BYTES = 1024 * 1024


def no_new_privileges():
    """Prevent learner code from gaining privileges through setuid executables."""
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(38, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "cannot enforce no_new_privs")


def reap_user(uid):
    """Kill all processes of the dedicated worker UID, including detached children.

    pidfds bind signals to the inspected process rather than a reusable PID.
    Failure to reach quiescence fails the job so the host destroys the sandbox.
    """
    pidfd_open = getattr(os, "pidfd_open", None)
    pidfd_signal = getattr(signal, "pidfd_send_signal", None)
    if not callable(pidfd_open) or not callable(pidfd_signal):
        raise RuntimeError("worker cleanup requires Linux pidfd support")
    deadline = time.monotonic() + 10
    while True:
        found = False
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            descriptor = None
            try:
                descriptor = pidfd_open(int(entry.name))
                status = (entry / "status").read_text()
                fields = dict(
                    line.split(":", 1) for line in status.splitlines() if ":" in line
                )
                uids = fields.get("Uid", "").split()
                if not uids or int(uids[0]) != uid:
                    continue
                # Zombies execute no work; their parent/init owns reaping.
                if fields.get("State", "").strip().startswith("Z"):
                    continue
                if entry.name == "1":
                    raise RuntimeError("worker init must run outside the learner UID")
                found = True
                pidfd_signal(descriptor, signal.SIGKILL)
            except (FileNotFoundError, ProcessLookupError):
                continue
            finally:
                if descriptor is not None:
                    os.close(descriptor)
        if not found:
            return
        if time.monotonic() >= deadline:
            raise TimeoutError("worker processes did not stop after job completion")
        time.sleep(0.02)


def read_output(stream):
    """Keep broker feedback bounded while reporting truncation explicitly."""
    stream.seek(0)
    data = stream.read(OUTPUT_BYTES + 1)
    return data[:OUTPUT_BYTES].decode("utf-8", errors="replace"), len(
        data
    ) > OUTPUT_BYTES


def run(source, seconds):
    """Execute one unprivileged Python file, then stop every remaining agent process."""
    if os.geteuid() != 0:
        raise PermissionError("worker launcher requires root")
    if not re.fullmatch(r"/app/[a-f0-9]{32}\.py", source):
        raise ValueError("invalid job source path")
    if type(seconds) is not int or seconds < 1:
        raise ValueError("job timeout must be a positive integer")
    user = pwd.getpwnam("agent")
    if user.pw_uid == 0:
        raise ValueError("worker account must be unprivileged")
    # Also clear leftovers before starting if the previous launcher was interrupted.
    reap_user(user.pw_uid)
    timed_out = False
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        process = None
        try:
            process = subprocess.Popen(
                [sys.executable, source],
                cwd="/app",
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                user=user.pw_uid,
                group=user.pw_gid,
                extra_groups=[],
                env={
                    **os.environ,
                    "HOME": user.pw_dir,
                    "USER": user.pw_name,
                    "LOGNAME": user.pw_name,
                },
                start_new_session=True,
                preexec_fn=no_new_privileges,
            )
            try:
                return_code = process.wait(timeout=seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                return_code = 124
        finally:
            try:
                reap_user(user.pw_uid)
            finally:
                if process is not None:
                    process.wait(timeout=5)
        out, out_truncated = read_output(stdout)
        err, err_truncated = read_output(stderr)
    return {
        "return_code": return_code,
        "stdout": out,
        "stderr": err,
        "timed_out": timed_out,
        "stdout_truncated": out_truncated,
        "stderr_truncated": err_truncated,
    }


if __name__ == "__main__":
    print(json.dumps(run(sys.argv[1], int(sys.argv[2]))))
