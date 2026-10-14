"""Restore an external CPU worker's files, remaining lifetime and SSH connection."""

import argparse
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

from sprint import Resources, Sandbox, Worker
from sprint.modal_worker import ModalWorker
from sprint.environments.modal import ModalEnvironment
import sprint.modal_worker as runtime


def main(records):
    records.mkdir(mode=0o700, parents=True, exist_ok=False)
    worker = Worker(
        sandbox=Sandbox(internet="full", setup=("printf base > /app/deleted.txt",)),
        resources=Resources(cpus=2, memory_mb=4096),
        backend="modal",
        shell=True,
        seconds=180,
        lifetime_seconds=300,
        state_paths=("/app", "/home/agent"),
    )

    def resource(name):
        state = records / name
        state.mkdir(mode=0o700)
        return ModalWorker(
            worker,
            state=state,
            connection_directory=records / "connection",
            deadline=time.time() + 300,
            trial=name,
            uid=os.getuid(),
            gid=os.getgid(),
        )

    def ssh(config, command):
        result = subprocess.run(
            ["ssh", "-F", str(config), "gpu", command],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"worker SSH failed ({result.returncode}): {result.stderr}"
            )
        return result.stdout.strip()

    original, restored = resource("original"), None
    try:
        config = original.connect()
        old_host = config.read_text()
        old_id = json.loads(original.record.read_text())["sandbox_id"]
        ssh(
            config,
            """set -eu
printf 7 > /app/counter.txt
printf '%s' '{"step":3,"lr":0.0002}' > /app/optimizer.json
ln /app/optimizer.json /app/optimizer-alias.json
rm /app/deleted.txt
python -c "from pathlib import Path; Path('/app/blocks.bin').write_bytes(b'q' * (17 * 1024 * 1024))"
""",
        )

        observation = original.observe()
        counter = next(
            row for row in observation["files"] if row["path"] == "/counter.txt"
        )
        if counter.get("text") != "7":
            raise RuntimeError("shared worker observation lost the counter fixture")

        async def reject_staging(*args, **kwargs):
            raise RuntimeError("capture fell back to a complete host archive")

        original.session.environment.download_file = reject_staging
        saved = original.checkpoint(records / "checkpoints", "mid-task")
        saved.verify()
        original.close()
        restored = resource("restored")
        upload_file = ModalEnvironment.upload_file

        async def reject_archive_upload(environment, source, target):
            if target.startswith("/.sprint-restore-") and target.endswith(".tar"):
                raise RuntimeError("restore staged a complete host archive")
            return await upload_file(environment, source, target)

        with patch.object(ModalEnvironment, "upload_file", reject_archive_upload):
            new_config = restored.restore(saved)
        if new_config != config or new_config.read_text() == old_host:
            raise RuntimeError("saved SSH path was not refreshed")
        new_id = json.loads(restored.record.read_text())["sandbox_id"]
        if new_id == old_id:
            raise RuntimeError("restore did not recreate the worker")
        result = json.loads(
            ssh(
                new_config,
                """python - <<'PY'
import hashlib, json
from pathlib import Path
root = Path('/app')
print(json.dumps({
    'counter': (root / 'counter.txt').read_text(),
    'fixture_bytes': (root / 'blocks.bin').stat().st_size,
    'fixture_sha256': hashlib.sha256((root / 'blocks.bin').read_bytes()).hexdigest(),
    'optimizer': json.loads((root / 'optimizer.json').read_text()),
    'deleted': (root / 'deleted.txt').exists(),
    'hardlink': (root / 'optimizer.json').stat().st_ino == (root / 'optimizer-alias.json').stat().st_ino,
}))
PY
""",
            )
        )
        if result != {
            "counter": "7",
            "fixture_bytes": 17 * 1024 * 1024,
            "fixture_sha256": hashlib.sha256(b"q" * (17 * 1024 * 1024)).hexdigest(),
            "optimizer": {"step": 3, "lr": 0.0002},
            "deleted": False,
            "hardlink": True,
        }:
            raise RuntimeError("restored application state differs")
        ssh(new_config, "printf 8 > /app/counter.txt")
        if ssh(new_config, "cat /app/counter.txt") != "8":
            raise RuntimeError("restored learner cannot continue its work")
        transfer_sizes = []
        environment = restored.session.environment
        download_stream = environment.download_stream

        async def measure_capture(source, consumer):
            size = await environment.exec(
                "stat -c %s " + source, user="root", timeout_sec=10
            )
            if size.return_code != 0:
                raise RuntimeError("could not measure capture transfer")
            transfer_sizes.append(int(size.stdout.strip()))
            return await download_stream(source, consumer)

        environment.download_stream = measure_capture
        incremental = restored.checkpoint(records / "checkpoints", "continued")
        incremental.verify()
        if len(transfer_sizes) != 1 or transfer_sizes[0] >= 1024 * 1024:
            raise RuntimeError("unchanged 17 MiB fixture was retransmitted")
        before = saved.metadata["trees"]["worker"]["app/blocks.bin"]
        after = incremental.metadata["trees"]["worker"]["app/blocks.bin"]
        if before != after:
            raise RuntimeError("incremental capture changed the fixture content")
        remaining = restored.deadline - time.time()
        if not 0 < remaining <= saved.metadata["remaining_seconds"]:
            raise RuntimeError("restore increased or exhausted worker lifetime")
        restored.quiesce()
        restored.observe()
        try:
            restored.connect()
        except RuntimeError:
            pass
        else:
            raise RuntimeError("quiesced worker reopened learner access")
    finally:
        original.close()
        if restored is not None:
            restored.close()
    if (
        original.session is not None
        or original.volume is not None
        or restored.session is not None
        or restored.volume is not None
    ):
        raise RuntimeError("resource cleanup did not complete")
    summary = {
        "passed": True,
        "worker_gpu": None,
        "checkpoint": str(saved.path),
        "saved_remaining_seconds": saved.metadata["remaining_seconds"],
        "restored_remaining_seconds": remaining,
        "restored_counter": result["counter"],
        "restored_optimizer_fixture": result["optimizer"],
        "deletion_preserved": not result["deleted"],
        "hardlink_preserved": result["hardlink"],
        "same_ssh_path_refreshed": True,
        "new_worker_identity": True,
        "continued_counter": 8,
        "cleanup_complete": True,
        "shared_observation_verified": True,
        "shared_quiesce_verified": True,
        "capture_without_host_archive": True,
        "restore_without_host_archive": True,
        "incremental_transfer_bytes": transfer_sizes[0],
        "unchanged_fixture_reused": True,
        "fixture_bytes": result["fixture_bytes"],
        "fixture_sha256": result["fixture_sha256"],
        "scope": "CPU worker and synthetic files; no GPU model state, training, native conversation, task-service recovery or benchmark evaluation.",
        "source_hashes": {
            name: hashlib.sha256(
                (Path(runtime.__file__).parent / name).read_bytes()
            ).hexdigest()
            for name in (
                "modal_worker.py",
                "state_transfer.py",
                "learner_ssh.py",
                "checkpoint_store.py",
                "streams.py",
                "environments/modal.py",
            )
        },
    }
    (records / "result.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=Path, required=True)
    main(parser.parse_args().records)
