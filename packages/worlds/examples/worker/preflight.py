"""Exercise learner queue -> persistent worker -> separate verifier."""

import argparse
import asyncio
from pathlib import Path
from uuid import uuid4

from worlds import (
    Agent,
    Budget,
    Experiment,
    Files,
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
    data = root / "data"
    data.mkdir()
    (data / "rows.txt").write_text("retained")
    private = root / "private"
    private.mkdir()
    (private / "evaluate.py").write_text(
        'def verify(context):\n    journal = context.records / "worlds-resources/train/jobs.jsonl"\n    return {"score": float((context.submission / "answer").read_text() == "retained" and "checkpoint-collected" in journal.read_text())}'
    )
    reference = root / "solve.sh"
    worker_check = ""
    if args.gpu:
        worker_check += 'import subprocess; print(subprocess.check_output(["nvidia-smi", "-L"]).decode())\n'
        worker_check += Path(__file__).with_name("gpu_probe.py").read_text() + "\n"
    if args.internet == "none":
        worker_check += 'import socket\ntry:\n socket.create_connection(("1.1.1.1",443),timeout=3)\nexcept OSError:\n print("outbound blocked")\nelse:\n raise RuntimeError("outbound unexpectedly allowed")\n'
    reference.write_text(
        """#!/bin/sh
set -eu
worlds-resource list
cat > /app/job.py <<'WORKER_CODE'
"""
        + worker_check
        + """from pathlib import Path
Path("checkpoint").write_text(Path("/resources/data/rows.txt").read_text())
try:
 Path("/resources/data/rows.txt").write_text("tampered")
except PermissionError:
 print("data write blocked")
else:
 raise RuntimeError("attached data was writable")
WORKER_CODE
worlds-resource run train /app/job.py --wait 120 > /app/first.json
python -c 'import json; assert json.load(open("/app/first.json"))["status"] == "completed"'
printf 'from pathlib import Path; print(Path("checkpoint").read_text())\\n' > /app/job.py
worlds-resource run train /app/job.py --wait 120 > /app/second.json
python -c 'import json; r=json.load(open("/app/second.json")); assert r["status"] == "completed"; assert r["stdout"].strip() == "retained"'
REQUEST=$(python -c 'import json; print(json.load(open("/app/first.json"))["request_id"])')
worlds-resource collect "$REQUEST" checkpoint --wait 120 > /app/collected.json
python -c 'import json,shutil; r=json.load(open("/app/collected.json")); assert r["status"] == "completed", r; shutil.copyfile(r["artifact"],"/app/submission/answer")'
"""
    )
    sandbox = Sandbox(internet=args.internet)
    experiment = Experiment(
        Task("Use the train worker and retain its checkpoint."),
        Validation(private, reference=reference, sandbox=sandbox),
        Agent("test", harness="oracle"),
        World(
            sandbox=sandbox,
            attachments={
                "data": Files(data),
                "train": Worker(sandbox=sandbox, resources=Resources(gpu=args.gpu)),
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
