"""Cancel one live Modal oracle trial and verify retained review records."""

import argparse
import asyncio
import json
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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=Path, required=True)
    asyncio.run(main(parser.parse_args().records))
