"""Exercise real containers with an empty artifact and a known reference solution."""

import argparse
import shutil
import tempfile
import asyncio
from uuid import uuid4

from pathlib import Path
from worlds import (
    Agent,
    Budget,
    Experiment,
    Task,
    Validation,
    Sandbox,
    World,
    Resources,
)


async def main(backend, internet, gpu):
    here = Path(__file__).parent
    root = Path(".worlds/preflight") / uuid4().hex[:12]
    with tempfile.TemporaryDirectory(prefix="worlds-preflight-") as temporary:
        workspace = Path(temporary) / "workspace"
        shutil.copytree(here / "workspace", workspace)
        if internet == "none":
            (workspace / "require-offline").touch()
        if gpu:
            (workspace / "require-gpu").touch()
        await check(here, root, workspace, backend, internet, gpu)


async def check(here, root, workspace, backend, internet, gpu):
    for harness in ("nop", "oracle"):
        experiment = Experiment(
            task=Task(
                "Calculate 6 × 7 and write answer.txt in your submission directory."
            ),
            validation=Validation(
                bundle=here / "validation",
                reference=here / "solve.sh",
                sandbox=Sandbox(internet=internet),
            ),
            world=World(
                sandbox=Sandbox(internet=internet),
                files=workspace,
                resources=Resources(gpu=gpu),
            ),
            agent=Agent(model="unused", harness=harness),
            budget=Budget(seconds=120),
        )
        result = await experiment.run(directory=root / harness, backend=backend)
        scores = result.rewards
        expected = 1 if harness == "oracle" else 0
        if scores != [expected]:
            raise RuntimeError(f"{harness} expected {[expected]}, got {scores}")
        print(f"{harness}: expected reward {expected}, observed {scores[0]}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", default="docker", choices=["docker", "modal"])
    parser.add_argument("--internet", default="none", choices=["none", "full"])
    parser.add_argument("--gpu", default=None)
    args = parser.parse_args()
    asyncio.run(main(args.backend, args.internet, args.gpu))
