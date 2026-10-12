"""Check that failed reference execution cannot be accepted as a scored solution."""

import asyncio
from pathlib import Path
from uuid import uuid4

from sprint import Agent, Budget, Experiment, Task, Validation


async def main():
    root = Path(".sprint/failure-preflight") / uuid4().hex
    private = root / "validation"
    private.mkdir(parents=True)
    (private / "evaluate.py").write_text(
        'def verify(context):\n    return {"score": 1.0}\n'
    )
    solution = root / "solve.sh"
    solution.write_text('#!/bin/sh\necho "deliberate reference failure" >&2\nexit 7\n')
    experiment = Experiment(
        Task("Exercise reference failure handling"),
        Validation(private, reference=solution),
        Agent("unused", harness="oracle"),
        budget=Budget(seconds=60),
    )
    result = await experiment.run(directory=root / "run", backend="modal")
    if len(result.trials) != 1 or result.rewards:
        raise RuntimeError(f"failed reference was scored: {result}")
    trial = result.trials[0]
    if (
        trial.status != "error"
        or not trial.error
        or trial.error.get("exception_type") != "OracleExecutionError"
    ):
        raise RuntimeError(f"reference error lost or misclassified: {trial}")
    exit_code = result.directory / trial.records / "agent/exit-code.txt"
    if exit_code.read_text() != "7":
        raise RuntimeError("reference exit code was not retained")
    print(root, "reference failure correctly retained without reward")


if __name__ == "__main__":
    asyncio.run(main())
