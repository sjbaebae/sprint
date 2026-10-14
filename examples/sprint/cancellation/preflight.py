"""Cancel one live Modal oracle trial and verify retained review records."""

import argparse
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

from sprint import Agent, Budget, Experiment, Task, Validation


async def main(records):
    records.mkdir(parents=True, mode=0o700, exist_ok=False)
    private = records / "validation"
    private.mkdir()
    (private / "evaluate.py").write_text('def verify(context): return {"score": 1.0}\n')
    solution = records / "solve.sh"
    solution.write_text(
        "#!/bin/sh\necho cancellation-fixture-started\ntouch /app/running\nsleep 120\n"
    )
    experiment = Experiment(
        task=Task("Wait for operator cancellation."),
        validation=Validation(private, reference=solution, seconds=10),
        agent=Agent("unused", harness="oracle"),
        budget=Budget(seconds=180),
    )
    run_dir = records / "run"
    async with experiment.start(directory=run_dir, backend="modal") as live:
        deadline = time.monotonic() + 30
        while True:
            state = await live.environment.exec("test -f /app/running", timeout_sec=5)
            if state.return_code == 0:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("oracle did not begin before cancellation")
            await asyncio.sleep(0.2)
        await live.stop()
    result = json.loads((run_dir / "result.json").read_text())
    if not result["trials"] or any(
        trial["status"] != "cancelled" or trial["reward"] is not None
        for trial in result["trials"]
    ):
        raise RuntimeError("cancelled trial was lost or received a reward")
    if not (run_dir / "review.html").is_file():
        raise RuntimeError("cancelled trial has no review page")
    if (run_dir / "cleanup-errors.json").exists():
        raise RuntimeError("cancelled trial cleanup is incomplete")
    summary = {
        "passed": True,
        "status": "cancelled",
        "reward": None,
        "review_retained": True,
        "interruption_retained": (run_dir / "interruption.json").is_file(),
        "cleanup_errors": False,
        "scope": "Modal CPU learner; oracle shell; explicit cancellation, no model inference or GPU.",
    }
    (records / "result.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary), flush=True)


async def host_loss(records):
    """Kill a native worker owner; recover only from its durable operator records."""
    import modal
    from sprint.modal_worker import cleanup_resources

    records.mkdir(parents=True, mode=0o700, exist_ok=False)
    recovery_root = records / "resources"
    state = recovery_root / "train"
    child_code = """
import asyncio, sys
from pathlib import Path
from sprint import Worker
from sprint.workers import WorkerSession
async def run():
    async with WorkerSession(Worker(shell=True, lifetime_seconds=300),
                             directory=Path(sys.argv[1]), backend="modal") as worker:
        await worker.start()
        await worker.shell("printf live > /app/host-loss-fixture")
        (Path(sys.argv[1]) / "ready").write_text("ready")
        await asyncio.sleep(300)
asyncio.run(run())
"""
    with (records / "owner.log").open("wb") as log:
        process = subprocess.Popen(
            [sys.executable, "-c", child_code, str(state)],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 150
            while not (state / "ready").is_file():
                if process.poll() is not None:
                    raise RuntimeError("worker owner failed; inspect owner.log")
                if time.monotonic() >= deadline:
                    raise TimeoutError("worker owner did not become ready")
                await asyncio.sleep(0.5)
            identity = json.loads((state / "worker.sandbox.json").read_text())
            sandbox = await modal.Sandbox.from_id.aio(identity["sandbox_id"])
            process.kill()
            process.wait(timeout=10)
            if await asyncio.to_thread(sandbox.poll) is not None:
                raise RuntimeError("worker did not survive its owner's abrupt death")
            await asyncio.to_thread(cleanup_resources, recovery_root)
            if await asyncio.to_thread(sandbox.poll) is None:
                raise RuntimeError("recorded cleanup left the worker running")
            retained = json.loads((state / "worker.sandbox.json").read_text())
            if not retained["terminated"] or not (state / "jobs.jsonl").is_file():
                raise RuntimeError("cleanup status or execution records were lost")
            summary = {
                "passed": True,
                "owner_exit_code": process.returncode,
                "worker_survived_owner": True,
                "cleanup_complete": True,
                "execution_records_retained": True,
                "scope": "SIGKILL of CPU worker-owner process; surviving operator cleanup. Not a whole-host outage or full learner trial.",
            }
            (records / "result.json").write_text(json.dumps(summary, indent=2))
            print(json.dumps(summary), flush=True)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            if state.is_dir():
                await asyncio.to_thread(cleanup_resources, recovery_root)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=Path, required=True)
    parser.add_argument(
        "--host-loss",
        action="store_true",
        help="Kill the native worker owner and recover recorded resources",
    )
    args = parser.parse_args()
    asyncio.run(host_loss(args.records) if args.host_loss else main(args.records))
