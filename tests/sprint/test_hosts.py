"""Explicit host placement, safe transport arguments and resource overrides."""

from pathlib import Path

import pytest

from sprint import (
    Agent,
    Env,
    Experiment,
    Files,
    Host,
    Resources,
    Task,
    Validation,
    Worker,
)
from sprint.host_runner import decode
from sprint.live import definition
from sprint.workers import WorkerSession


def test_host_ssh_arguments_do_not_invoke_a_local_shell():
    host = Host("root@host", identity=Path("/keys/key with spaces"), port=1234)
    args = host.command("echo 'literal $HOME'")
    assert args[-2:] == ["root@host", "echo 'literal $HOME'"]
    assert args[args.index("-i") + 1] == "/keys/key with spaces"
    assert "BatchMode=yes" in args
    for target in ["-oProxyCommand=bad", "root@host bad", ""]:
        with pytest.raises(ValueError):
            Host(target)


def test_host_secrets_are_not_in_repr():
    host = Host("root@host", env={"OPENAI_API_KEY": "private-value"})
    assert "private-value" not in repr(host)


def test_declarative_remote_roundtrip_preserves_definition(tmp_path):
    experiment = Experiment(
        Task("task"),
        Validation(tmp_path / "private"),
        Agent("model"),
        Env(
            attachments={
                "data": Files(tmp_path / "data"),
                "train": Worker(backend="modal", resources=Resources(gpu="L4")),
            }
        ),
    )
    assert definition(decode(definition(experiment))) == definition(experiment)


def test_worker_placement_overrides_inherited_backend(tmp_path):
    worker = Worker(backend="modal")
    session = WorkerSession(worker, directory=tmp_path, backend="docker")
    assert session.backend == "modal"
    assert (
        WorkerSession(Worker(), directory=tmp_path, backend="docker").backend
        == "docker"
    )
    with pytest.raises(ValueError):
        Worker(backend="not-a-provider")


@pytest.mark.asyncio
async def test_experiment_dispatches_to_selected_host(tmp_path, monkeypatch):
    experiment = Experiment(Task("task"), Validation(tmp_path), Agent("model"))
    captured = {}

    async def remote(self, selected, **kwargs):
        captured.update(kwargs)
        assert selected is experiment
        return "remote-result"

    monkeypatch.setattr(Host, "run", remote)
    result = await experiment.run(
        directory=tmp_path, host=Host("root@host"), backend="modal"
    )
    assert result == "remote-result"
    assert captured["backend"] == "modal" and captured["trials"] == 1


@pytest.mark.asyncio
async def test_installed_wheel_staging_regenerates_record(tmp_path, monkeypatch):
    import csv
    import io
    import zipfile
    from importlib.metadata import PackagePath
    from types import SimpleNamespace
    import sprint.hosts as hosts

    installed = tmp_path / "installed"
    files = {
        "sprint/__init__.py": b"VERSION = 'test'\n",
        "sprint-1.0.dist-info/METADATA": b"Name: sprint\nVersion: 1.0\n",
        "sprint-1.0.dist-info/RECORD": b"old record",
    }
    for name, data in files.items():
        target = installed / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    distribution = SimpleNamespace(
        version="1.0",
        files=[PackagePath(name) for name in files],
        locate_file=lambda path: installed / path,
    )
    monkeypatch.setattr(hosts, "__file__", str(tmp_path / "other/a/b/hosts.py"))
    monkeypatch.setattr(
        hosts.importlib.metadata, "distribution", lambda name: distribution
    )
    wheel = await hosts.runtime_wheel(tmp_path)
    with zipfile.ZipFile(wheel) as archive:
        assert archive.testzip() is None
        rows = list(
            csv.reader(
                io.StringIO(archive.read("sprint-1.0.dist-info/RECORD").decode())
            )
        )
        assert any(
            row[0] == "sprint/__init__.py" and row[1].startswith("sha256=")
            for row in rows
        )
        assert rows[-1] == ["sprint-1.0.dist-info/RECORD", "", ""]
