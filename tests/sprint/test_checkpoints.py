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


def test_bounded_checkpoint_preserves_and_validates_action_budget(tmp_path):
    from sprint.live import validate_resume_limits

    experiment = Experiment(
        Task("task"),
        Validation(tmp_path / "private"),
        Agent("model", harness="bounded-codex", options={"max_turns": 10}),
    )
    validate_resume_limits(experiment, {"remaining_seconds": 30, "actions_used": 7})
    for used in (None, -1, 10):
        with pytest.raises(ValueError, match="action budget"):
            validate_resume_limits(
                experiment, {"remaining_seconds": 30, "actions_used": used}
            )


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
    first = put_tree(
        store, tmp_path, {"app/a": b"same", "app/b": b"old", "app/deleted": b"remove"}
    )
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
        {"tree": "learner", "path": "app/deleted", "status": "deleted"},
    ]
    b.verify()
    archive = tmp_path / "restored.tar"
    store.export_archive(second, archive)
    with tarfile.open(archive) as tar:
        assert tar.extractfile("app/a").read() == b"same"
        assert tar.extractfile("app/b").read() == b"new"
        assert "app/deleted" not in tar.getnames()


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


def test_streamed_export_checks_complete_file_across_chunks(tmp_path, monkeypatch):
    monkeypatch.setattr("sprint.checkpoint_store.CHUNK_BYTES", 4093)
    store = CheckpointStore(tmp_path / "store")
    payload = bytes(range(256)) * 513
    tree = put_tree(store, tmp_path, {"app/model": payload, "app/empty": b""})
    archive = tmp_path / "restored.tar"
    store.export_archive(tree, archive)
    with tarfile.open(archive) as stream:
        assert stream.extractfile("app/model").read() == payload
        assert stream.extractfile("app/empty").read() == b""
    tree["app/model"]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="integrity"):
        store.export_archive(tree, archive)


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


def test_capture_preserves_cache_hardlinks_without_following_symlinks(tmp_path):
    import os
    import subprocess
    from sprint.remote_state import pack

    work = tmp_path / "work"
    work.mkdir()
    original = work / "cache-file"
    original.write_bytes(b"cached package bytes")
    os.link(original, work / "installed-file")
    private = tmp_path / "private-file"
    private.write_text("must not be captured")
    (work / "outside-link").symlink_to(private)
    source = tmp_path / "source.tar"
    pack([str(work)], source)
    store = CheckpointStore(tmp_path / "store")
    entries = store.import_archive(source)
    assert sum(entry["kind"] == "hardlink" for entry in entries.values()) == 1
    assert sum(entry["kind"] == "file" for entry in entries.values()) == 1
    archive = tmp_path / "restored.tar"
    store.export_archive(entries, archive)
    destination = tmp_path / "restored"
    destination.mkdir()
    subprocess.run(["tar", "-xpf", str(archive), "-C", str(destination)], check=True)
    restored = destination / str(work).lstrip("/")
    assert (restored / "cache-file").read_bytes() == b"cached package bytes"
    assert (restored / "cache-file").stat().st_ino == (
        restored / "installed-file"
    ).stat().st_ino
    assert (restored / "outside-link").readlink() == private


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
    from sprint.agents.installed.bounded_codex import BoundedCodex

    store = CheckpointStore(tmp_path / "store")
    experiment = Experiment(
        Task("task"),
        Validation(tmp_path / "private"),
        Agent("model", harness="bounded-codex", options={"max_turns": 10}),
    )
    release = put_tree(
        store, tmp_path, {"release/task.toml": b"schema_version = '1.4'\n"}
    )
    session_id = "00000000-0000-4000-8000-000000000001"
    native = f"logs/agent/sessions/rollout-2026-10-13T00-00-00-{session_id}.jsonl"
    records = [
        {"type": "session_meta", "payload": {"id": session_id}},
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "Compare filtering with the baseline.",
                    }
                ],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "output_text", "text": "Keep the optimizer fixed."}
                ],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "call_id": "call_1",
                "name": "exec_command",
                "arguments": '{"cmd":"python baseline.py"}',
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": "Baseline accuracy: 0.40",
            },
        },
    ]
    conversation = "".join(json.dumps(record) + "\n" for record in records).encode()
    learner = put_tree(
        store,
        tmp_path,
        {native: conversation + b'{"partial":'},
    )
    cp = store.publish(
        "one",
        {
            "trees": {"release": release, "learner": learner},
            "definition": definition(experiment),
            "remaining_seconds": 42.5,
            "native_session": native,
            "actions_used": 7,
        },
    )
    _, release_dir, session, seconds = prepare_restore(
        experiment, cp, tmp_path / "restore"
    )
    assert release_dir.is_dir() and seconds == 42.5
    assert Path(session).read_bytes() == conversation
    resumed_agent = BoundedCodex(
        logs_dir=tmp_path / "agent",
        load_trajectory=session,
        max_turns=10,
        actions_used=cp.metadata["actions_used"],
    )
    assert resumed_agent.actions_used == 7
    assert (
        json.loads((tmp_path / "restore/resume.json").read_text())["remaining_seconds"]
        == 42.5
    )
    different = Experiment(Task("different"), experiment.validation, experiment.agent)
    with pytest.raises(ValueError, match="different experiment"):
        prepare_restore(different, cp, tmp_path / "bad")


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_broker", [False, True])
async def test_failed_capture_unfreezes_and_does_not_publish(
    tmp_path, monkeypatch, failed_broker
):
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
    if failed_broker:
        import asyncio

        async def failed_poll():
            raise RuntimeError("transfer failed")

        task = asyncio.create_task(failed_poll())
        await asyncio.sleep(0)
        run.broker = SimpleNamespace(gate=asyncio.Lock(), task=task)
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


@pytest.mark.parametrize("remaining", [True, float("nan"), float("inf"), -1, 3601])
def test_resume_rejects_invalid_or_increased_trial_budget(tmp_path, remaining):
    from sprint.live import validate_resume_limits

    experiment = Experiment(Task("task"), Validation(tmp_path), Agent("model"))
    with pytest.raises(ValueError, match="trial budget"):
        validate_resume_limits(experiment, {"remaining_seconds": remaining})


def test_resume_rejects_freed_spending_and_increased_worker_time(tmp_path):
    from sprint import Budget, Worker
    from sprint.live import validate_resume_limits

    experiment = Experiment(
        Task("task"),
        Validation(tmp_path),
        Agent("model"),
        Env(attachments={"train": Worker(hourly_cost=2)}),
        Budget(worker_spend=2),
    )
    metadata = {
        "remaining_seconds": 100,
        "workers": {"train": {"remaining_seconds": 50}},
        "broker": {"seen": [], "completed": {}, "reservations": {"train": 2}},
    }
    validate_resume_limits(experiment, metadata)
    metadata["broker"]["reservations"]["train"] = 0
    with pytest.raises(ValueError, match="declared lifetime"):
        validate_resume_limits(experiment, metadata)
    metadata["broker"]["reservations"]["train"] = 2
    metadata["workers"]["train"]["remaining_seconds"] = 3601
    with pytest.raises(ValueError, match="worker budget"):
        validate_resume_limits(experiment, metadata)


def test_restore_rejects_out_of_scope_state_before_creating_run(tmp_path):
    store = CheckpointStore(tmp_path / "store")
    experiment = Experiment(Task("task"), Validation(tmp_path), Agent("model"))
    learner = put_tree(store, tmp_path, {"etc/shadow": b"out of scope"})
    checkpoint = store.publish(
        "escape",
        {
            "definition": definition(experiment),
            "remaining_seconds": 10,
            "trees": {"learner": learner, "release": {}},
        },
    )
    destination = tmp_path / "restore"
    with pytest.raises(ValueError, match="declared roots"):
        prepare_restore(experiment, checkpoint, destination)
    assert not destination.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["oracle", "bounded-codex"])
async def test_restored_fractional_budget_uses_integer_modal_lifetime(
    tmp_path, monkeypatch, harness
):
    import sprint
    import sprint.live as live
    from sprint.backends.runtime import run

    experiment = Experiment(
        Task("task"),
        Validation(tmp_path, backend="docker"),
        Agent("test", harness=harness),
    )
    captured = []

    class Captured(Exception):
        pass

    async def create(config):
        captured.append(config)
        raise Captured()

    monkeypatch.setattr(sprint.Job, "create", create)
    monkeypatch.setattr(
        live,
        "prepare_restore",
        lambda *args: (
            SimpleNamespace(metadata={"actions_used": 7}),
            tmp_path,
            None,
            42.5,
        ),
    )
    with pytest.raises(Captured):
        await run(
            experiment,
            directory=tmp_path,
            backend="modal",
            trials=1,
            parallel=1,
            resume_from="checkpoint",
        )
    assert captured[0].verifier.environment.type.value == "docker"
    assert not captured[0].verifier.environment.kwargs
    lifetime = captured[0].environment.kwargs["sandbox_timeout_secs"]
    assert type(lifetime) is int and lifetime == 943
    if harness == "bounded-codex":
        assert captured[0].agents[0].kwargs["seconds"] == 42.5
        assert captured[0].agents[0].kwargs["actions_used"] == 7


@pytest.mark.parametrize("change", ["queue", "missing-worker", "changed-worker"])
def test_resume_resource_helper_compatibility_before_materialization(tmp_path, change):
    import sprint.live as live
    from sprint import Worker

    experiment = Experiment(
        Task("task"),
        Validation(tmp_path / "private"),
        Agent("unused", harness="oracle"),
        env=Env(attachments={"train": Worker()}),
    )
    sources = {
        "release/environment/resource_client.py": "resource_client.py",
        "release/workers/train/worker_run.py": "worker_run.py",
        "release/workers/train/worker_export.py": "worker_export.py",
    }
    values = {
        name: Path(live.__file__).with_name(helper).read_bytes()
        for name, helper in sources.items()
    }
    if change == "queue":
        values["release/environment/resource_client.py"] = (
            b"old destructive queue reader"
        )
    elif change == "missing-worker":
        del values["release/workers/train/worker_run.py"]
    elif change == "changed-worker":
        values["release/workers/train/worker_export.py"] = b"different export protocol"
    store = CheckpointStore(tmp_path / "store")
    release = put_tree(store, tmp_path, values)
    learner = put_tree(store, tmp_path, {"app/state": b"saved"})
    checkpoint = store.publish(
        "one",
        {
            "definition": definition(experiment),
            "remaining_seconds": 30,
            "trees": {"release": release, "learner": learner},
            "workers": {},
            "broker": {"seen": [], "completed": {}, "reservations": {}},
        },
    )
    target = tmp_path / "restore"
    with pytest.raises(ValueError, match="checkpoint resource runtime differs"):
        prepare_restore(experiment, checkpoint, target)
    assert not target.exists()


def test_compiled_resource_release_passes_resume_compatibility(tmp_path):
    from sprint import Worker

    private = tmp_path / "private"
    private.mkdir()
    (private / "evaluate.py").write_text('def verify(context): return {"score": 1.0}')
    experiment = Experiment(
        Task("task"),
        Validation(private),
        Agent("unused", harness="oracle"),
        env=Env(attachments={"train": Worker()}),
    )
    task = experiment.compile(tmp_path / "compiled")
    store = CheckpointStore(tmp_path / "store")
    release = put_tree(
        store,
        tmp_path,
        {
            "release/" + str(path.relative_to(task)): path.read_bytes()
            for path in task.rglob("*")
            if path.is_file()
        },
    )
    learner = put_tree(store, tmp_path, {"app/state": b"saved"})
    checkpoint = store.publish(
        "one",
        {
            "definition": definition(experiment),
            "remaining_seconds": 30,
            "trees": {"release": release, "learner": learner},
            "workers": {},
            "broker": {"seen": [], "completed": {}, "reservations": {}},
        },
    )
    _, restored, _, _ = prepare_restore(experiment, checkpoint, tmp_path / "restore")
    assert (restored / "workers/train/worker_run.py").read_bytes() == (
        task / "workers/train/worker_run.py"
    ).read_bytes()


def test_restore_refuses_request_lineage_larger_than_budget(tmp_path):
    from sprint import Budget
    from sprint.live import validate_resume_limits

    experiment = Experiment(
        Task("task"),
        Validation(tmp_path / "private"),
        Agent("unused"),
        budget=Budget(resource_requests=1),
    )
    with pytest.raises(ValueError, match="request lineage"):
        validate_resume_limits(
            experiment,
            {
                "remaining_seconds": 30,
                "workers": {},
                "broker": {"seen": ["a" * 32, "b" * 32]},
            },
        )
