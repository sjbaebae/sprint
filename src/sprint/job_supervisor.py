"""Durable, idempotent Sprint child supervisor on one operator-owned CPU Pod.

Invoke `start RECORD --config CONFIG` once or repeatedly. `status RECORD` reports the
recorded process identity and exit status; a stale PID is never treated as live.
Only this root-owned supervisor writes lifecycle records.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def write(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2))
    temporary.chmod(0o600)
    temporary.replace(path)


def identity(pid):
    try:
        stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return {
            "pid": pid,
            "start_ticks": stat[19],
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        }
    except (FileNotFoundError, ProcessLookupError):
        return None


def status(record):
    value = json.loads(record.read_text())
    if value.get("state") == "running":
        current = identity(value["process"]["pid"])
        if current != value["process"]:
            return {
                **value,
                "state": "lost",
                "reason": "supervisor process identity missing",
            }
    return value


def supervise(config, record):
    root = Path.cwd()
    value = {
        "state": "running",
        "config": str(config),
        "started_at": time.time(),
        "process": identity(os.getpid()),
    }
    write(record, value)
    env = dict(
        os.environ,
        PYTHONPATH=str(root),
    )
    with record.with_suffix(".log").open("ab") as log:
        result = subprocess.run(
            [str(root / ".venv/bin/sprint"), "run", "-c", str(config)],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    outcome = {
        **value,
        "state": "failed",
        "exit_code": result.returncode,
        "finished_at": time.time(),
    }
    if result.returncode == 0:
        try:
            definition = json.loads(config.read_text())
            result_path = (
                Path(definition["jobs_dir"]) / definition["job_name"] / "result.json"
            )
            job = json.loads(result_path.read_text())
            stats = job["stats"]
            outcome["job_result"] = str(result_path)
            outcome["trial_counts"] = {
                name: stats[name] for name in ("n_completed_trials", "n_errored_trials")
            }
            if stats["n_errored_trials"]:
                outcome["failure"] = "trial_errors"
            elif (
                job["n_total_trials"] < 1
                or stats["n_completed_trials"] != job["n_total_trials"]
            ):
                outcome["failure"] = "incomplete_trials"
            else:
                outcome["state"] = "completed"
        except (OSError, ValueError, KeyError, TypeError):
            outcome["failure"] = "missing_or_invalid_job_result"
    write(record, outcome)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["start", "status", "_supervise"])
    p.add_argument("record", type=Path)
    p.add_argument("--config", type=Path)
    a = p.parse_args()
    record = a.record.resolve()
    if a.action == "status":
        print(json.dumps(status(record)))
    elif a.action == "_supervise":
        supervise(a.config.resolve(), record)
    else:
        if os.geteuid() != 0:
            raise SystemExit("root operator required")
        record.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Exclusive claim persists across crashes; lost trials require operator review.
        claim = record.with_suffix(".claim")
        try:
            fd = os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            print(
                json.dumps(
                    status(record)
                    if record.exists()
                    else {"state": "claimed_pending_review"}
                )
            )
            return
        os.close(fd)
        if a.config is None or not a.config.is_file():
            claim.unlink()
            raise SystemExit("existing config required")
        with record.with_suffix(".supervisor.log").open("ab") as log:
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "sprint.job_supervisor",
                    "_supervise",
                    str(record),
                    "--config",
                    str(a.config.resolve()),
                ],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        print(json.dumps({"state": "starting", "supervisor_pid": child.pid}))


if __name__ == "__main__":
    main()
