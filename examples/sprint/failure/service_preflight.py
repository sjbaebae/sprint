"""Inject one terminal broker failure and check independent Modal trial survival."""

import argparse
import asyncio
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from sprint import Agent, Budget, Env, Experiment, Sandbox, Task, Validation, Worker
from sprint.broker import ResourceBroker


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=("modal", "docker"), default="modal")
    args = parser.parse_args()
    sandbox = Sandbox(internet="full" if args.backend == "docker" else "none")
    root = Path(".sprint/service-failure-preflight") / uuid4().hex
    private = root / "validation"
    private.mkdir(parents=True)
    (private / "evaluate.py").write_text(
        'def verify(context):\n    return {"score": 1.0}\n'
    )
    solution = root / "solve.sh"
    solution.write_text("#!/bin/sh\nsleep 8\nmkdir -p /app/submission\n")
    experiment = Experiment(
        Task("Exercise trial-scoped resource failure handling"),
        Validation(private, reference=solution, sandbox=sandbox),
        Agent("unused", harness="oracle"),
        env=Env(sandbox=sandbox, attachments={"train": Worker()}),
        budget=Budget(seconds=120),
    )
    original = ResourceBroker.serve
    attached = 0

    async def injected(broker):
        nonlocal attached
        attached += 1
        if attached == 1:
            await asyncio.sleep(2)
            raise RuntimeError("preflight: terminal resource service failure")
        await original(broker)

    with patch.object(ResourceBroker, "serve", injected):
        result = await experiment.run(
            directory=root / "run", backend=args.backend, trials=2, parallel=2
        )
    errors = [trial for trial in result.trials if trial.status == "error"]
    if len(errors) != 1 or result.rewards != [1.0]:
        raise RuntimeError(f"trial failure isolation failed: {result}")
    if not errors[0].error or "terminal resource service failure" not in str(
        errors[0].error
    ):
        raise RuntimeError("original service failure was not retained")
    print(root, "one failed trial retained; independent trial scored 1.0")


if __name__ == "__main__":
    asyncio.run(main())
