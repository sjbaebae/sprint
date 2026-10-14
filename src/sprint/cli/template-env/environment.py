"""Edit this definition and validation/evaluate.py; launch with sprint env run."""

from pathlib import Path

from sprint import Agent, Budget, Env, Experiment, Sandbox, Task, Validation

ROOT = Path(__file__).resolve().parent

experiment = Experiment(
    task=Task("Compute 6 × 7. Write the answer to /app/submission/answer.txt."),
    validation=Validation(ROOT / "validation"),  # Performance only; no judge.
    env=Env(
        sandbox=Sandbox(
            internet="full",
            setup=(
                "apt-get update && apt-get install -y --no-install-recommends nodejs npm ca-certificates && rm -rf /var/lib/apt/lists/*",
                "npm install -g @openai/codex@0.162.1",
            ),
        )
    ),
    agent=Agent(
        "gpt-6-astra",
        harness="bounded-codex",
        options={
            "version": "0.162.1",
            "max_turns": 200,
            "reasoning_effort": "medium",
            "reasoning_summary": "detailed",
        },
    ),
    budget=Budget(seconds=3600),
)
