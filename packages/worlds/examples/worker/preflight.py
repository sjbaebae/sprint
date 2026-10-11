"""Exercise learner queue -> persistent worker -> separate verifier."""

import argparse
import asyncio
from pathlib import Path
from uuid import uuid4

from worlds import (
    Agent,
    Budget,
    Experiment,
    Resources,
    Sandbox,
    Task,
    Validation,
    Worker,
    World,
)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=["docker", "modal"], default="modal")
    parser.add_argument("--internet", choices=["none", "full"], default="none")
    parser.add_argument("--gpu", default=None)
    args = parser.parse_args()
    root = Path(".worlds/worker-preflight") / uuid4().hex
    root.mkdir(parents=True)
    private = root / "private"
    private.mkdir()
    (private / "evaluate.py").write_text(
        'def verify(context): return {"score": float((context.submission / "answer").read_text() == "retained")}'
    )
    reference = root / "solve.sh"
    reference.write_text("""#!/bin/sh
set -eu
worlds-resource list
printf 'from pathlib import Path; Path("checkpoint").write_text("retained")\\n' > /app/job.py
worlds-resource run train /app/job.py --wait 120 > /app/first.json
python -c 'import json; assert json.load(open("/app/first.json"))["status"] == "completed"'
printf 'from pathlib import Path; print(Path("checkpoint").read_text())\\n' > /app/job.py
worlds-resource run train /app/job.py --wait 120 > /app/second.json
python -c 'import json; from pathlib import Path; r=json.load(open("/app/second.json")); assert r["status"] == "completed"; Path("/app/submission/answer").write_text(r["stdout"].strip())'
""")
    sandbox = Sandbox(internet=args.internet)
    experiment = Experiment(
        Task("Use the train worker and retain its checkpoint."),
        Validation(private, reference=reference, sandbox=sandbox),
        Agent("test", harness="oracle"),
        World(
            sandbox=sandbox,
            attachments={
                "train": Worker(sandbox=sandbox, resources=Resources(gpu=args.gpu))
            },
        ),
        Budget(seconds=300),
    )
    result = await experiment.run(directory=root / "run", backend=args.backend)
    print(root, result.rewards)
    if result.rewards != [1.0]:
        raise RuntimeError("worker queue preflight failed; inspect persisted records")


if __name__ == "__main__":
    asyncio.run(main())
