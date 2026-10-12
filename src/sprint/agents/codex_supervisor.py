"""Root-owned Codex supervisor: enforce action/time limits and clean up learner processes."""

import argparse
import ctypes
import json
import os
import pwd
import selectors
import secrets
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def write_status(directory_fd, name, status):
    """Replace a directory entry without following learner symlinks/hardlinks."""
    temporary = ".sprint-budget-" + secrets.token_hex(16)
    try:
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o444,
            dir_fd=directory_fd,
        )
        with os.fdopen(fd, "w") as stream:
            json.dump(status, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


def enforce_no_new_privileges():
    # Docker and Modal must enforce the same non-root child boundary.
    if ctypes.CDLL(None, use_errno=True).prctl(38, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "could not enforce no_new_privs")


def stop_trial_processes(uid):
    """Dedicated container per trial: also stop children that called setsid()."""
    if uid == 0 or not Path("/proc/self/status").exists():
        raise RuntimeError("trial cleanup requires a non-root UID and Linux procfs")
    killed = set()
    deadline = time.monotonic() + 2
    while True:
        remaining = []
        for path in Path("/proc").iterdir():
            if not path.name.isdigit():
                continue
            try:
                if path.stat().st_uid != uid:
                    continue
                status = (path / "status").read_text()
                if any(
                    line.startswith("State:") and "\tZ" in line
                    for line in status.splitlines()
                ):
                    continue
                remaining.append(int(path.name))
            except (FileNotFoundError, ProcessLookupError):
                continue
        if not remaining:
            return len(killed)
        for pid in remaining:
            try:
                os.kill(pid, signal.SIGKILL)
                killed.add(pid)
            except ProcessLookupError:
                pass
        if time.monotonic() >= deadline:
            raise RuntimeError("learner processes survived cleanup")
        time.sleep(0.02)


class TurnCounter:
    def __init__(self):
        self.ids = set()

    def observe(self, line):
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            return len(self.ids)
        if event.get("type") in ("item.started", "item.completed"):
            item = event.get("item", {})
            if item.get("type") in (
                "agent_message",
                "command_execution",
                "mcp_tool_call",
                "web_search",
                "file_change",
                "collab_tool_call",
            ):
                if item.get("id") is not None:
                    self.ids.add(item["id"])
        return len(self.ids)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--max-turns", type=int, required=True)
    p.add_argument("--seconds", type=float, required=True)
    p.add_argument("--uid", type=int, required=True)
    p.add_argument("--gid", type=int, required=True)
    p.add_argument("--status", required=True)
    p.add_argument("--command", required=True)
    p.add_argument("--live-status", default="/run/sprint/budget.json")
    a = p.parse_args()
    if os.geteuid() != 0 or a.max_turns < 1 or a.seconds <= 0:
        p.error("requires root and positive limits")
    env = os.environ.copy()
    account = pwd.getpwuid(a.uid)
    env.update(HOME=account.pw_dir, USER=account.pw_name, LOGNAME=account.pw_name)
    status_path = Path(a.status)
    status_fd = os.open(
        status_path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    )
    live_path = Path(a.live_status)
    live_path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    live_fd = os.open(live_path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    child = subprocess.Popen(
        ["/bin/bash", "-o", "pipefail", "-c", a.command],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        user=a.uid,
        group=a.gid,
        extra_groups=[],
        start_new_session=True,
        preexec_fn=enforce_no_new_privileges,
    )
    selector = selectors.DefaultSelector()
    selector.register(child.stdout, selectors.EVENT_READ)
    counter = TurnCounter()
    pending = b""
    started = time.monotonic()
    reason = "completed"
    last_status = 0

    def progress():
        elapsed = time.monotonic() - started
        remaining = max(0, a.max_turns - len(counter.ids))
        write_status(
            live_fd,
            live_path.name,
            {
                "state": "running",
                "actions_used": len(counter.ids),
                "actions_remaining": remaining,
                "maximum_actions": a.max_turns,
                "seconds_remaining": max(0, a.seconds - elapsed),
                "maximum_seconds": a.seconds,
                "submit_now": remaining <= 20 or a.seconds - elapsed <= 600,
                "counting": "unique tool/action and assistant-message items; reasoning excluded",
            },
        )

    progress()
    try:
        while child.poll() is None or selector.get_map():
            if time.monotonic() - last_status >= 1:
                progress()
                last_status = time.monotonic()
            if time.monotonic() - started >= a.seconds:
                reason = "time_limit"
                break
            for key, _ in selector.select(timeout=0.2):
                block = os.read(key.fileobj.fileno(), 65536)
                if not block:
                    selector.unregister(key.fileobj)
                    continue
                pending += block
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    try:
                        event = json.loads(line)
                    except ValueError:
                        event = {}
                    if event:
                        event["_recorded_at_unix"] = time.time()
                        event["_recorded_at_utc"] = datetime.fromtimestamp(
                            event["_recorded_at_unix"], timezone.utc
                        ).isoformat(timespec="milliseconds")
                        event["_elapsed_seconds"] = time.monotonic() - started
                        sys.stdout.write(json.dumps(event) + "\n")
                    else:
                        sys.stdout.buffer.write(line + b"\n")
                    sys.stdout.flush()
                    if event.get("type") in ("error", "turn.failed"):
                        reason = "agent_error"
                        break
                    if counter.observe(line) >= a.max_turns:
                        reason = "turn_limit"
                        break
            if reason != "completed":
                break
    finally:
        # Also clean up background children after normal CLI completion.
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            child.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()
        status = {
            "reason": reason,
            "turns": len(counter.ids),
            "max_turns": a.max_turns,
            "turn_definition": "unique native tool/action or assistant-message items; reasoning excluded",
            "seconds": time.monotonic() - started,
            "max_seconds": a.seconds,
            "exit_code": child.returncode,
        }
        try:
            status["detached_processes_killed"] = stop_trial_processes(a.uid)
            status["cleanup_complete"] = True
        except Exception:
            status["cleanup_complete"] = False
            reason = "cleanup_failed"
            status["reason"] = reason
        write_status(
            live_fd,
            live_path.name,
            {
                **status,
                "state": "finished",
                "actions_remaining": max(0, a.max_turns - len(counter.ids)),
            },
        )
        os.close(live_fd)
        try:
            write_status(status_fd, status_path.name, status)
        finally:
            os.close(status_fd)
    # Budget exhaustion ends submission normally so Sprint still runs the verifier.
    if reason == "completed" and child.returncode:
        raise SystemExit(child.returncode)
    if reason == "agent_error":
        raise SystemExit(1)
    if reason == "cleanup_failed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
