"""Exercise learner queue -> persistent worker -> separate verifier."""

import argparse
import asyncio
from pathlib import Path
from uuid import uuid4

from sprint import (
    Agent,
    Host,
    Budget,
    Experiment,
    Files,
    Resources,
    Sandbox,
    Task,
    Validation,
    Worker,
    Env,
)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=["docker", "modal"], default="modal")
    parser.add_argument("--internet", choices=["none", "full"], default="none")
    parser.add_argument("--gpu", default=None)
    parser.add_argument(
        "--shell", action="store_true", help="Check persistent worker shell access"
    )
    parser.add_argument("--host", help="SSH operator host, e.g. root@Runpod-IP")
    parser.add_argument("--identity", type=Path)
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--host-uv", default="uv")
    args = parser.parse_args()
    root = Path(".sprint/worker-preflight") / uuid4().hex
    root.mkdir(parents=True)
    data = root / "data"
    data.mkdir()
    (data / "rows.txt").write_text("retained")
    private = root / "private"
    private.mkdir()
    (private / "evaluate.py").write_text(
        'def verify(context):\n    journal = context.records / "sprint-resources/train/jobs.jsonl"\n    return {"score": float((context.submission / "answer").read_text() == "retained" and "checkpoint-collected" in journal.read_text())}'
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
sprint-resource list
printf 'uploaded' > /app/input.txt
sprint-resource upload train /app/input.txt inputs/value.txt --wait 120 > /app/upload.json
python -c 'import json; r=json.load(open("/app/upload.json")); assert r["status"] == "completed", r'
cat > /app/job.py <<'WORKER_CODE'
"""
        + worker_check
        + """from pathlib import Path
assert Path("/app/inputs/value.txt").read_text() == "uploaded"
Path("checkpoint").write_text(Path("/resources/data/rows.txt").read_text())
try:
 Path("/resources/data/rows.txt").write_text("tampered")
except PermissionError:
 print("data write blocked")
else:
 raise RuntimeError("attached data was writable")
import subprocess, sys
status = Path("/proc/self/status").read_text()
assert "NoNewPrivs:" in status and next(line for line in status.splitlines() if line.startswith("NoNewPrivs:")).split()[1] == "1"
child = subprocess.Popen([sys.executable, "-c", "import time; from pathlib import Path; time.sleep(1); Path('/app/leaked-process').write_text('leaked')"], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
Path("child-pid").write_text(str(child.pid))
WORKER_CODE
sprint-resource run train /app/job.py --wait 120 > /app/first.json
python -c 'import json; assert json.load(open("/app/first.json"))["status"] == "completed"'
cat > /app/job.py <<'WORKER_CODE'
from pathlib import Path
import time
time.sleep(1.2)
assert not Path("/app/leaked-process").exists(), "detached process survived prior job"
assert not Path("/proc/" + Path("child-pid").read_text()).exists(), "detached child was not reaped"
print(Path("checkpoint").read_text())
WORKER_CODE
sprint-resource run train /app/job.py --wait 120 > /app/second.json
python -c 'import json; r=json.load(open("/app/second.json")); assert r["status"] == "completed"; assert r["stdout"].strip() == "retained"'
REQUEST=$(python -c 'import json; print(json.load(open("/app/first.json"))["request_id"])')
sprint-resource collect "$REQUEST" checkpoint --wait 120 > /app/collected.json
python -c 'import json,shutil; r=json.load(open("/app/collected.json")); assert r["status"] == "completed", r; shutil.copyfile(r["artifact"],"/app/submission/answer")'
cat > /app/timeout.py <<'WORKER_CODE'
import time
print("partial output before timeout", flush=True)
time.sleep(5)
WORKER_CODE
sprint-resource run deadline /app/timeout.py --wait 120 > /app/timeout.json
python -c 'import json; r=json.load(open("/app/timeout.json")); assert r["status"] == "failed", r; assert r["timed_out"] is True and r["return_code"] == 124, r; assert "partial output before timeout" in r["stdout"], r'
sprint-resource run deadline /app/timeout.py --wait 120 > /app/closed.json
python -c 'import json; r=json.load(open("/app/closed.json")); assert r["status"] == "error" and "closed" in r["message"], r'
"""
    )
    if args.shell:
        shell_checks = """sprint-resource exec train 'echo persistent > /app/shell-marker; nohup sleep 120 > /app/shell-process.log 2>&1 & echo $! > /app/shell-pid' --wait 120 > /app/shell-first.json
python -c 'import json; assert json.load(open("/app/shell-first.json"))["status"] == "completed"'
sprint-resource exec train 'test "$(cat /app/shell-marker)" = persistent && kill -0 "$(cat /app/shell-pid)" && kill "$(cat /app/shell-pid)"' --wait 120 > /app/shell-second.json
python -c 'import json; assert json.load(open("/app/shell-second.json"))["status"] == "completed"'
"""
        reference.write_text(
            reference.read_text().replace("set -eu\n", "set -eu\n" + shell_checks, 1)
        )
    sandbox = Sandbox(internet=args.internet)
    experiment = Experiment(
        Task("Use the train worker and retain its checkpoint."),
        Validation(private, reference=reference, sandbox=sandbox),
        Agent("test", harness="oracle"),
        Env(
            sandbox=sandbox,
            attachments={
                "data": Files(data),
                "train": Worker(
                    sandbox=sandbox, resources=Resources(gpu=args.gpu), shell=args.shell
                ),
                "deadline": Worker(sandbox=sandbox, seconds=1),
            },
        ),
        Budget(seconds=300),
    )
    host = (
        Host(args.host, identity=args.identity, port=args.port, uv=args.host_uv)
        if args.host
        else None
    )
    result = await experiment.run(
        directory=root / "run", backend=args.backend, host=host
    )
    print(root, result.rewards)
    if result.rewards != [1.0]:
        raise RuntimeError("worker queue preflight failed; inspect persisted records")


if __name__ == "__main__":
    asyncio.run(main())
