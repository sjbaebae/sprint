"""Kill a CPU task service mid-call; resume without repeating its side effect."""

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

from sprint.request_queue import LocalRequestQueue
import sprint.request_queue as queue_runtime

IDS = [f"{index:032x}" for index in range(1, 4)]


def service(root, *, resume):
    def prepare(request, request_id):
        def execute():
            with (root / "effects.jsonl").open("a") as stream:
                stream.write(json.dumps({"request_id": request_id}) + "\n")
            if request_id == IDS[1]:
                time.sleep(60)
            if request_id == IDS[2]:
                queue.stop()
            return {"request_id": request_id}

        return execute

    queue = LocalRequestQueue(
        root / "rpc",
        root / "state",
        deadline=time.time() + 90,
        prepare=prepare,
        completed=lambda result: None,
        failure_feedback=lambda error, stage: {"failure_stage": stage},
        poll_seconds=0.01,
        resume=resume,
    )
    queue.run()


def check(root):
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    (root / "state").mkdir()
    requests = root / "rpc/requests"
    requests.mkdir(parents=True)
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--records",
        str(root),
        "--child",
    ]
    child = subprocess.Popen(command)
    try:
        for request_id in IDS[:2]:
            (requests / (request_id + ".json")).write_text('{"kind":"run"}')
            deadline = time.monotonic() + 10
            while True:
                effects = root / "effects.jsonl"
                if effects.exists() and request_id in effects.read_text():
                    break
                if child.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError("service did not execute the expected request")
                time.sleep(0.02)
            if request_id == IDS[0]:
                while not (root / "state" / (request_id + ".result.json")).exists():
                    if child.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError("first request did not finish")
                    time.sleep(0.02)
        child.kill()
        child.wait(timeout=5)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
    (requests / (IDS[2] + ".json")).write_text('{"kind":"run"}')
    subprocess.run(command + ["--resume"], check=True, timeout=10)
    effects = [
        json.loads(line)["request_id"]
        for line in (root / "effects.jsonl").read_text().splitlines()
    ]
    feedback = [
        json.loads((root / "rpc/responses" / (request_id + ".json")).read_text())
        for request_id in IDS
    ]
    if effects != IDS or [value["status"] for value in feedback] != [
        "complete",
        "failed",
        "complete",
    ]:
        raise RuntimeError("service recovery repeated or lost a request")
    if feedback[1].get("outcome_known") is not False:
        raise RuntimeError("interrupted request did not expose uncertainty")
    result = {
        "passed": True,
        "effects": effects,
        "feedback": feedback,
        "python": sys.version,
        "queue_sha256": hashlib.sha256(
            Path(queue_runtime.__file__).read_bytes()
        ).hexdigest(),
        "preflight_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    (root / "result.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=Path, required=True)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--resume", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        service(args.records, resume=args.resume)
    else:
        check(args.records)
