"""Allocated-host consent must precede any account cleanup or workspace mutation."""

from types import SimpleNamespace

import pytest

from sprint.environments import allocated


@pytest.mark.asyncio
async def test_allocated_refuses_busy_account_and_existing_lease_without_killing(
    tmp_path, monkeypatch
):
    environment = object.__new__(allocated.AllocatedEnvironment)
    environment.owned = False
    environment.reap = False
    environment.state = tmp_path
    environment.lease = tmp_path / "lease"
    environment.account = SimpleNamespace(pw_uid=1000)

    def forbidden_cleanup(uid):
        raise AssertionError(
            "refusing an unrelated account must not kill its processes"
        )

    monkeypatch.setattr(allocated, "live_processes", lambda uid: [123])
    monkeypatch.setattr(allocated, "stop_trial_processes", forbidden_cleanup)
    environment.lease.mkdir()
    with pytest.raises(FileExistsError):
        await environment.start()
    assert environment.lease.exists()
    environment.lease.rmdir()
    with pytest.raises(RuntimeError, match="already has running"):
        await environment.start()
    assert not environment.lease.exists()
    assert not environment.owned


@pytest.mark.asyncio
async def test_allocated_rejects_operator_state_roots_before_creating_run(
    tmp_path, monkeypatch
):
    from pathlib import Path
    from sprint import Agent, Env, Experiment, Sandbox, Task, Validation, Worker
    from sprint.backends.runtime import run

    monkeypatch.setattr(
        allocated, "allocated_config", lambda: (object(), Path("/app/work"), tmp_path)
    )
    experiment = Experiment(
        Task("task", submission="/app/work/submission"),
        Validation(tmp_path, backend="modal"),
        Agent("unused", harness="oracle"),
        Env(sandbox=Sandbox(internet="full"), state_paths=("/etc",)),
    )
    destination = tmp_path / "run"
    with pytest.raises(ValueError, match="learner harness directories"):
        await run(
            experiment, directory=destination, backend="allocated", trials=1, parallel=1
        )
    assert not destination.exists()
    with pytest.raises(ValueError, match="learner-only"):
        Worker(backend="allocated")
