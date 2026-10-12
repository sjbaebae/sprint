"""Checkpoint integrity, incremental storage and failure semantics."""

import io
import json
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from sprint import Agent, Env, Experiment, Task, Validation
from sprint.checkpoint_store import Checkpoint, CheckpointStore
from sprint.live import LiveRun, definition, prepare_restore


def put_tree(store, tmp_path, values):
    archive = tmp_path / "export.tar"
    with tarfile.open(archive, "w") as tar:
        for name, value in values.items():
            info = tarfile.TarInfo(name)
            if isinstance(value, tuple):
                info.type, info.linkname = tarfile.SYMTYPE, value[0]
                tar.addfile(info)
            else:
                info.size = len(value)
                tar.addfile(info, io.BytesIO(value))
    return store.import_archive(archive)


def test_incremental_complete_state_and_diff(tmp_path):
    store = CheckpointStore(tmp_path / "store")
    first = put_tree(store, tmp_path, {"app/a": b"same", "app/b": b"old"})
    a = store.publish("first", {"trees": {"learner": first}})
    before = len(list(store.blobs.iterdir()))
    second = put_tree(
        store, tmp_path, {"app/a": b"same", "app/b": b"new", "app/c": b"added"}
    )
    b = store.publish("second", {"trees": {"learner": second}})
    assert len(list(store.blobs.iterdir())) == before + 2
    assert a.diff(b) == [
        {"tree": "learner", "path": "app/b", "status": "modified"},
        {"tree": "learner", "path": "app/c", "status": "added"},
    ]
    b.verify()
    archive = tmp_path / "restored.tar"
    store.export_archive(second, archive)
    with tarfile.open(archive) as tar:
        assert tar.extractfile("app/a").read() == b"same"
        assert tar.extractfile("app/b").read() == b"new"


def test_corrupt_or_missing_blob_never_restores(tmp_path):
    store = CheckpointStore(tmp_path / "store")
    tree = put_tree(store, tmp_path, {"app/a": b"state"})
    checkpoint = store.publish("one", {"trees": {"learner": tree}})
    blob = next(store.blobs.iterdir())
    blob.write_bytes(b"corrupted")
    with pytest.raises(ValueError, match="corrupt"):
        checkpoint.verify()
    blob.unlink()
    with pytest.raises(ValueError, match="missing"):
        checkpoint.verify()


def test_refuses_path_traversal_links_and_special_files(tmp_path):
    store = CheckpointStore(tmp_path / "store")
    with pytest.raises(ValueError, match="unsafe"):
        put_tree(store, tmp_path, {"../escape": b"x"})
    with pytest.raises(ValueError, match="traverses"):
        put_tree(store, tmp_path, {"app/link": ("/etc",), "app/link/passwd": b"x"})
    # A link without descendants is stored as metadata, never followed on host.
    before = set(store.blobs.iterdir())
    tree = put_tree(store, tmp_path, {"app/link": ("/etc",)})
    assert tree["app/link"]["target"] == "/etc"
    assert set(store.blobs.iterdir()) == before


def test_manifest_corruption_and_duplicate_names(tmp_path):
    store = CheckpointStore(tmp_path / "store")
    checkpoint = store.publish("one", {"trees": {}})
    with pytest.raises(FileExistsError):
        store.publish("one", {"trees": {}})
    checkpoint.path.write_text(
        checkpoint.path.read_text().replace(
            '"schema_version": 1', '"schema_version": 2'
        )
    )
    with pytest.raises(ValueError, match="integrity"):
        Checkpoint(checkpoint.path)


def test_restore_pins_definition_budget_and_trims_interrupted_session(tmp_path):
    store = CheckpointStore(tmp_path / "store")
    experiment = Experiment(
        Task("task"), Validation(tmp_path / "private"), Agent("model")
    )
    release = put_tree(
        store, tmp_path, {"release/task.toml": b"schema_version = '1.4'\n"}
    )
    learner = put_tree(
        store,
        tmp_path,
        {"logs/agent/sessions/one.jsonl": b'{"type":"session_meta"}\n{"partial":'},
    )
    cp = store.publish(
        "one",
        {
            "trees": {"release": release, "learner": learner},
            "definition": definition(experiment),
            "remaining_seconds": 42.5,
            "native_session": "logs/agent/sessions/one.jsonl",
        },
    )
    _, release_dir, session, seconds = prepare_restore(
        experiment, cp, tmp_path / "restore"
    )
    assert release_dir.is_dir() and seconds == 42.5
    assert Path(session).read_text() == '{"type":"session_meta"}\n'
    assert (
        json.loads((tmp_path / "restore/resume.json").read_text())["remaining_seconds"]
        == 42.5
    )
    different = Experiment(Task("different"), experiment.validation, experiment.agent)
    with pytest.raises(ValueError, match="different experiment"):
        prepare_restore(different, cp, tmp_path / "bad")


@pytest.mark.asyncio
async def test_failed_capture_unfreezes_and_does_not_publish(tmp_path, monkeypatch):
    import time
    import sprint.live as live

    experiment = Experiment(
        Task("task"), Validation(tmp_path / "private"), Agent("model")
    )
    run = LiveRun(experiment, directory=tmp_path / "run", backend="fake")
    run.attached(SimpleNamespace(), None, tmp_path, seconds=100)
    run.started = time.monotonic()
    calls = []

    async def helper(environment):
        return "/tmp/helper.py"

    async def checked(environment, command, **kwargs):
        calls.append(command)
        if command.endswith(" freeze"):
            return '{"123":"start"}'
        return ""

    async def capture(*args):
        raise RuntimeError("transfer failed")

    monkeypatch.setattr(live, "remote_helper", helper)
    monkeypatch.setattr(live, "checked", checked)
    monkeypatch.setattr(run, "_capture", capture)
    with pytest.raises(RuntimeError, match="transfer failed"):
        await run.checkpoint("one")
    assert any("unfreeze" in command for command in calls)
    assert not list((tmp_path / "run/checkpoints").glob("*.json"))


def test_state_paths_refuse_overlap_and_pseudo_filesystems():
    for roots in [("/",), ("/app", "/app/nested"), ("/proc",), ("/app/../etc",)]:
        with pytest.raises(ValueError):
            Env(state_paths=roots)


def test_remote_pack_preserves_files_and_refuses_special_state(tmp_path):
    import os
    from sprint.remote_state import pack, validate_roots

    root = tmp_path / "workspace"
    root.mkdir()
    (root / "data").write_text("checkpoint")
    (root / "link").symlink_to("/etc")
    archive = tmp_path / "state.tar"
    pack([str(root)], str(archive))
    with tarfile.open(archive) as stream:
        entry = stream.getmember(str(root).lstrip("/") + "/link")
        assert entry.issym() and entry.linkname == "/etc"
    os.mkfifo(root / "pipe")
    with pytest.raises(ValueError, match="nonportable"):
        pack([str(root)], str(tmp_path / "invalid.tar"))
    alias = tmp_path / "alias"
    alias.symlink_to(root)
    with pytest.raises(ValueError, match="traverses"):
        validate_roots([str(alias / "nested")])
