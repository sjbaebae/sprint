"""Run from the repository root: uv run --package sprint python examples/sprint/answer/environment.py."""

import asyncio
from pathlib import Path

from sprint import Agent, Budget, Experiment, Task, Validation, World

experiment = Experiment(
    task=Task("Calculate 6 × 7 and write answer.txt in your submission directory."),
    validation=Validation(bundle=Path(__file__).parent / "validation"),
    agent=Agent(model="gpt-6-astra", harness="codex"),
    budget=Budget(seconds=120),
    world=World(files=Path(__file__).parent / "workspace"),
)

if __name__ == "__main__":
    asyncio.run(experiment.run(directory=Path(".sprint/answer")))
