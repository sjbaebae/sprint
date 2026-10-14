"""Real logical-state restore with a worker, request lineage and remaining budget."""

import argparse
import asyncio
import json
import shlex
import tempfile
import tarfile
from unittest.mock import patch
from pathlib import Path
from uuid import uuid4

from sprint import (
    Agent,
    Budget,
    Env,
    Experiment,
    Sandbox,
    Service,
    Task,
    Validation,
    Worker,
)


PENDING_REQUEST_ID = "d" * 32


async def main(*, ssh=False, allocated=False, service=False, pending=False):
    if pending and not service:
        raise ValueError("--pending requires --service")
    backend = "allocated" if allocated else "modal"
    workspace = "/app"
    state_paths = None
    if allocated:
        from sprint.environments.allocated import allocated_config

        _, root, _ = allocated_config()
        workspace = str(root)
        state_paths = (
            workspace,
            "/home/agent",
            "/logs/agent",
            "/resources/control",
            "/installed-agent",
        )
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
        attachments = {}
        if service:
            bundle = root / "service"
            bundle.mkdir()
            (bundle / "service.py").write_text(
                "async def handle(context, request):\n"
                '    path = context.state / "calls.txt"\n'
                '    fixture = context.state / "large-private-fixture.bin"\n'
                '    if path.exists() and not fixture.is_file(): raise RuntimeError("private fixture was not restored")\n'
                '    if not fixture.exists(): fixture.write_bytes(b"q" * (17 * 1024 * 1024))\n'
                "    count = int(path.read_text()) + 1 if path.exists() else 1\n"
                "    path.write_text(str(count))\n"
                '    context.record("counter", {"count": count})\n'
                '    return {"count": count}\n'
                "async def finalize(context):\n"
                '    worker = await context.worker("compute")\n'
                '    observation = await worker.observe("/app")\n'
                '    context.record("final-audit", {"observed": isinstance(observation, dict)})\n'
            )
            attachments["counter"] = Service(bundle)
            evaluator = private / "evaluate.py"
            evaluator.write_text(
                "import json\n"
                "def verify(context):\n"
                '    p = context.submission / "answer.txt"\n'
                '    evidence = context.records / "sprint-resources/service-counter.jsonl"\n'
                "    events = [json.loads(line) for line in evidence.read_text().splitlines()]\n"
                '    counts = [event["payload"]["count"] for event in events if event["event"] == "counter"]\n'
                '    audits = [event for event in events if event["event"] == "final-audit"]\n'
                '    private = context.records / "sprint-resources/service-counter/calls.txt"\n'
                '    valid = counts == [1, 2] and not private.exists() and bool(audits) and audits[-1]["payload"]["observed"]\n'
                '    valid = valid and (context.submission / "service-valid").is_file()\n'
                '    return {"score": float(valid and p.is_file() and p.read_text().strip() == "17")}\n'
            )
            text = solution.read_text().replace(
                "  touch ready\n",
                "  echo '{}' > service-input.json\n"
                "  sprint-resource call counter service-input.json --wait 60 > service-first.json\n"
                "  touch ready\n",
            )
            text += (
                "\nsprint-resource call counter service-input.json --wait 60 > service-second.json\n"
                "python - <<'SERVICECHECK'\n"
                "import json\n"
                'assert json.load(open("service-first.json"))["result"]["count"] == 1\n'
                'assert json.load(open("service-second.json"))["result"]["count"] == 2\n'
                "SERVICECHECK\n"
                "touch /app/submission/service-valid\n"
            )
            if pending:
                text = text.replace(
                    "sprint-resource call counter service-input.json --wait 60 > service-second.json",
                    "sprint-resource status "
                    + "d" * 32
                    + " --wait 60 > service-second.json",
                )
            solution.write_text(text)
        solution.write_text(
            solution.read_text()
            .replace("cd /app\n", "cd " + workspace + "\n")
            .replace(
                'Path("/app/submission/answer.txt")',
                'Path("' + workspace + '/submission/answer.txt")',
            )
        )
        experiment = Experiment(
            Task("Resume the staged calculation", submission=workspace + "/submission"),
            Validation(private, reference=solution, backend="modal"),
            Agent("unused", harness="oracle"),
            Env(
                sandbox=Sandbox(
                    internet="full" if ssh or allocated else "none",
                    setup=(
                        "apt-get update && apt-get install -y --no-install-recommends openssh-client",
                    )
                    if ssh and not allocated
                    else (),
                ),
                **({"state_paths": state_paths} if state_paths is not None else {}),
                files=public,
                attachments={
                    **attachments,
                    "compute": Worker(
                        lifetime_seconds=300,
                        backend="modal",
                        shell=ssh,
                        sandbox=Sandbox(internet="full" if ssh else "none"),
                    ),
                },
            ),
            Budget(seconds=240),
        )
        archive_open = tarfile.open

        def reject_release_staging(name=None, *args, **kwargs):
            if isinstance(name, (str, Path)) and Path(name).name == "release.tar":
                raise RuntimeError("native recovery staged a complete host archive")
            return archive_open(name, *args, **kwargs)

        with patch("tarfile.open", side_effect=reject_release_staging):
            async with experiment.start(
                directory=directory / "original", backend=backend
            ) as run:
                for _ in range(80):
                    result = await run.environment.exec(
                        "test -f " + workspace + "/ready", user="root", timeout_sec=10
                    )
                    if result.return_code == 0:
                        break
                    await asyncio.sleep(1)
                else:
                    raise RuntimeError("original did not reach its staged checkpoint")
                if pending:
                    capture = run._capture
                    payload = json.dumps(
                        {
                            "service": "counter",
                            "input": {},
                        }
                    )

                    async def capture_with_pending(environment, roots, store):
                        # Inject at the quiescent boundary: admitted calls have drained,
                        # but this valid queued request has not yet been admitted.
                        if environment is run.environment:
                            command = (
                                "printf %s "
                                + shlex.quote(payload)
                                + " > /resources/control/requests/"
                                + PENDING_REQUEST_ID
                                + ".json"
                            )
                            injected = await environment.exec(
                                command, user="root", timeout_sec=10
                            )
                            if injected.return_code != 0:
                                raise RuntimeError("could not inject pending request")
                        return await capture(environment, roots, store)

                    run._capture = capture_with_pending
                checkpoint = await run.checkpoint("staged")
                if pending:
                    if PENDING_REQUEST_ID in checkpoint.metadata["broker"]["seen"]:
                        raise RuntimeError("pending request was already admitted")
                    saved_path = (
                        "resources/control/requests/" + PENDING_REQUEST_ID + ".json"
                    )
                    if saved_path not in checkpoint.metadata["trees"]["learner"]:
                        raise RuntimeError("pending request was not captured")
                print("checkpoint:", checkpoint.path, flush=True)
            restored = await experiment.run(
                directory=directory / "restored",
                backend=backend,
                resume_from=checkpoint,
            )
            if restored.rewards != [1.0]:
                raise RuntimeError(
                    f"restore failed: {restored.rewards}; inspect {directory}"
                )
            print("restored reward:", restored.rewards, flush=True)
            print(
                "remaining budget:",
                checkpoint.metadata["remaining_seconds"],
                flush=True,
            )
            print(
                "stored chunks:",
                len(list(checkpoint.store.blobs.iterdir())),
                flush=True,
            )
            print(json.dumps({"directory": str(directory), "reward": restored.rewards}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ssh",
        action="store_true",
        help="Exercise direct worker SSH and refreshed host pins",
    )
    parser.add_argument(
        "--allocated",
        action="store_true",
        help="Use the opted-in native Linux learner account",
    )
    parser.add_argument(
        "--service",
        action="store_true",
        help="Recover private task-service state with worker state",
    )
    parser.add_argument(
        "--pending",
        action="store_true",
        help="Inject a queued service request at the snapshot boundary",
    )
    args = parser.parse_args()
    asyncio.run(
        main(
            ssh=args.ssh,
            allocated=args.allocated,
            service=args.service,
            pending=args.pending,
        )
    )
