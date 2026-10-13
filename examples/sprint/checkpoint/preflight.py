"""Real logical-state restore with a worker, request lineage and remaining budget."""

import argparse
import asyncio
import json
import tempfile
from pathlib import Path
from uuid import uuid4

from sprint import Agent, Budget, Env, Experiment, Sandbox, Task, Validation, Worker


async def main(*, ssh=False):
    directory = Path(".sprint/checkpoint-preflight") / uuid4().hex
    with tempfile.TemporaryDirectory(prefix="sprint-checkpoint-example-") as temporary:
        root = Path(temporary)
        public, private = root / "workspace", root / "validation"
        public.mkdir()
        private.mkdir()
        (private / "evaluate.py").write_text(
            'def verify(context):\n    p = context.submission / "answer.txt"\n    return {"score": float(p.is_file() and p.read_text().strip() == "17")}\n'
        )
        solution = root / "solve.sh"
        solution.write_text("""#!/bin/sh
set -eu
cd /app
if [ ! -f ready ]; then
  echo 7 > counter.txt
  echo 'from pathlib import Path; Path("/app/value.txt").write_text("9")' > job.py
  sprint-resource run compute job.py --wait 60 > first.json
  python -c 'import json; r=json.load(open("first.json")); assert r["status"] == "completed", r'
  touch ready
  sleep 90
fi
# These files and the completed request must come from the saved state.
python - <<'PYCODE'
import json
from pathlib import Path
r = json.loads(Path("first.json").read_text())
Path("original-id.txt").write_text(r["request_id"])
PYCODE
sprint-resource collect "$(cat original-id.txt)" value.txt --wait 60 > collected.json
python - <<'PYCODE'
import json
from pathlib import Path
result = json.loads(Path("collected.json").read_text())
assert Path(result["artifact"]).read_text() == "9"
Path("next.py").write_text('from pathlib import Path; p=Path("/app/value.txt"); p.write_text(str(int(p.read_text())+1))')
PYCODE
sprint-resource run compute next.py --wait 60 > second.json
python - <<'PYCODE'
import json
from pathlib import Path
r = json.loads(Path("second.json").read_text())
Path("next-id.txt").write_text(r["request_id"])
PYCODE
sprint-resource collect "$(cat next-id.txt)" value.txt --wait 60 > last.json
python - <<'PYCODE'
import json
from pathlib import Path
r = json.loads(Path("last.json").read_text())
answer = int(Path("counter.txt").read_text()) + int(Path(r["artifact"]).read_text())
Path("/app/submission/answer.txt").write_text(str(answer))
PYCODE
""")
        if ssh:
            # Reuse the same state calculation through the learner's ordinary SSH.
            solution.write_text("""#!/bin/sh
set -eu
cd /app
if [ ! -f ready ]; then
  echo 7 > counter.txt
  sprint-resource connect compute --wait 60 > connection.json
  python - <<'PYCODE'
import json
from pathlib import Path
r = json.loads(Path("connection.json").read_text())
assert r["status"] == "completed", r
Path("ssh-config.txt").write_text(r["ssh_config"])
PYCODE
  ssh -F "$(cat ssh-config.txt)" gpu 'echo 9 > /app/value.txt'
  touch ready
  sleep 90
fi
# Restore refreshes this saved config with the new worker's pinned endpoint.
ssh -F "$(cat ssh-config.txt)" gpu 'value=$(cat /app/value.txt); echo "$((value + 1))" > /app/value.txt'
ssh -F "$(cat ssh-config.txt)" gpu 'cat /app/value.txt' > worker-value.txt
python - <<'PYCODE'
from pathlib import Path
answer = int(Path("counter.txt").read_text()) + int(Path("worker-value.txt").read_text())
Path("/app/submission/answer.txt").write_text(str(answer))
PYCODE
""")
        experiment = Experiment(
            Task("Resume the staged calculation"),
            Validation(private, reference=solution, backend="modal"),
            Agent("unused", harness="oracle"),
            Env(
                sandbox=Sandbox(
                    internet="full" if ssh else "none",
                    setup=(
                        "apt-get update && apt-get install -y --no-install-recommends openssh-client",
                    )
                    if ssh
                    else (),
                ),
                files=public,
                attachments={
                    "compute": Worker(
                        lifetime_seconds=300,
                        shell=ssh,
                        sandbox=Sandbox(internet="full" if ssh else "none"),
                    )
                },
            ),
            Budget(seconds=240),
        )
        async with experiment.start(
            directory=directory / "original", backend="modal"
        ) as run:
            for _ in range(80):
                result = await run.environment.exec(
                    "test -f /app/ready", user="root", timeout_sec=10
                )
                if result.return_code == 0:
                    break
                await asyncio.sleep(1)
            else:
                raise RuntimeError("original did not reach its staged checkpoint")
            checkpoint = await run.checkpoint("staged")
            print("checkpoint:", checkpoint.path, flush=True)
        restored = await experiment.run(
            directory=directory / "restored", backend="modal", resume_from=checkpoint
        )
        if restored.rewards != [1.0]:
            raise RuntimeError(
                f"restore failed: {restored.rewards}; inspect {directory}"
            )
        print("restored reward:", restored.rewards, flush=True)
        print("remaining budget:", checkpoint.metadata["remaining_seconds"], flush=True)
        print("stored chunks:", len(list(checkpoint.store.blobs.iterdir())), flush=True)
        print(json.dumps({"directory": str(directory), "reward": restored.rewards}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ssh",
        action="store_true",
        help="Exercise direct worker SSH and refreshed host pins",
    )
    asyncio.run(main(ssh=parser.parse_args().ssh))
