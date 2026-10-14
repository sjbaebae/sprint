"""Shared logical-state transfer for native trials and external workers."""

import json
import shlex
import tempfile
from pathlib import Path
from uuid import uuid4


async def remote_helper(environment):
    """Upload the trusted stdlib helper outside learner-writable roots."""
    remote = "/.sprint-state-" + uuid4().hex + ".py"
    await environment.upload_file(Path(__file__).with_name("remote_state.py"), remote)
    return remote


async def checked(environment, command, *, timeout=600):
    """Require successful root transfer operations and preserve diagnostics."""
    result = await environment.exec(command, user="root", timeout_sec=timeout)
    if result.return_code != 0:
        raise RuntimeError("state transfer failed: " + str(result.stderr))
    return result.stdout or ""


async def restore_tree(environment, checkpoint, tree, roots):
    """Restore approved mutable roots, preserving deletions and file metadata."""
    entries = checkpoint.metadata["trees"][tree]
    prefixes = tuple(root.lstrip("/") for root in roots)
    for name in entries:
        if not any(name == p or name.startswith(p + "/") for p in prefixes):
            raise ValueError("checkpoint state escapes its declared roots")
    remote = "/.sprint-restore-" + uuid4().hex + ".tar"
    helper = await remote_helper(environment)
    try:
        if hasattr(environment, "upload_stream"):
            await environment.upload_stream(
                remote, lambda stream: checkpoint.store.export_archive(entries, stream)
            )
        else:
            with tempfile.TemporaryDirectory(prefix="sprint-restore-") as temporary:
                archive = Path(temporary) / "state.tar"
                checkpoint.store.export_archive(entries, archive)
                await environment.upload_file(archive, remote)
        await checked(
            environment,
            f"python -I -S {shlex.quote(helper)} validate-roots {shlex.quote(json.dumps(roots))}",
        )
        # Validate and finish the upload before replacing any mutable roots.
        remove = " ".join(shlex.quote(root) for root in roots)
        await checked(
            environment,
            f"rm -rf -- {remove} && tar -xpf {shlex.quote(remote)} -C /",
        )
    finally:
        await checked(
            environment, "rm -f -- " + shlex.join([remote, helper]), timeout=30
        )


async def capture_tree(environment, roots, store):
    """Capture already-quiescent roots into the shared content-addressed store."""
    reusable = store.known_files()
    helper = await remote_helper(environment)
    remote = "/.sprint-state-" + uuid4().hex + ".tar"
    inventory = remote + ".json"
    try:
        with tempfile.TemporaryDirectory(prefix="sprint-inventory-") as temporary:
            source = Path(temporary) / "known.json"
            source.write_text(
                json.dumps({key: value["size"] for key, value in reusable.items()})
            )
            await environment.upload_file(source, inventory)
        await checked(
            environment,
            f"python -I -S {shlex.quote(helper)} pack {shlex.quote(json.dumps(roots))} {shlex.quote(remote)} {shlex.quote(inventory)}",
        )
        if hasattr(environment, "download_stream"):
            return await environment.download_stream(
                remote, lambda stream: store.import_archive(stream, reusable=reusable)
            )
        with tempfile.TemporaryDirectory(prefix="sprint-capture-") as temporary:
            archive = Path(temporary) / "state.tar"
            await environment.download_file(remote, archive)
            return store.import_archive(archive, reusable=reusable)
    finally:
        await checked(
            environment,
            "rm -f -- " + shlex.join([remote, helper, inventory]),
            timeout=30,
        )
