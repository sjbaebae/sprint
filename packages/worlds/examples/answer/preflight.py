"""Exercise real containers with an empty artifact and a known reference solution."""

import asyncio

# Public networking is explicit for this toy Docker Desktop check only.
# Default authored environments remain offline; unsupported policies fail closed.
from pathlib import Path
from worlds import Agent, Budget, Experiment, Task, Validation, Sandbox, World


async def main():
    here = Path(__file__).parent
    for harness in ("nop", "oracle"):
        experiment = Experiment(
            task=Task(
                "Calculate 6 × 7 and write answer.txt in your submission directory."
            ),
            validation=Validation(
                bundle=here / "validation",
                reference=here / "solve.sh",
                sandbox=Sandbox(internet="full"),
            ),
            world=World(sandbox=Sandbox(internet="full")),
            agent=Agent(model="unused", harness=harness),
            budget=Budget(seconds=120),
        )
        result = await experiment.run(
            directory=Path(".worlds/preflight-public") / harness
        )
        scores = [
            trial.verifier_result.rewards["reward"]
            for trial in result.trial_results
            if trial.verifier_result and trial.verifier_result.rewards
        ]
        expected = 1 if harness == "oracle" else 0
        if scores != [expected]:
            raise RuntimeError(f"{harness} expected {[expected]}, got {scores}")
        print(f"{harness}: expected reward {expected}, observed {scores[0]}")


if __name__ == "__main__":
    asyncio.run(main())
