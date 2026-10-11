"""Worker ownership and checkpoint reuse without provider credentials."""

from types import SimpleNamespace

import pytest

from worlds import Worker
from worlds.workers import WorkerSession


class FakeEnvironment:
    def __init__(self, **kwargs):
        self.config = kwargs
        self.starts = 0
        self.stops = 0
        self.fail = False
        self.sources = []

    async def start(self, **kwargs):
        self.starts += 1

    async def stop(self, **kwargs):
        self.stops += 1

    async def upload_file(self, source, target):
        self.sources.append(source.read_text())

    async def exec(self, command, **kwargs):
        assert kwargs["user"] == "agent"
        if self.fail:
            raise TimeoutError("deadline exceeded")
        return SimpleNamespace(return_code=0, stdout="ok", stderr="")


@pytest.mark.asyncio
async def test_worker_reuses_container_and_destroys_it(tmp_path):
    environments = []

    def factory(**kwargs):
        env = FakeEnvironment(**kwargs)
        environments.append(env)
        return env

    async with WorkerSession(
        Worker(), directory=tmp_path / "worker", factory=factory
    ) as session:
        first = await session.run("print(1)")
        second = await session.run("print(2)")
        assert first["job_id"] != second["job_id"]
        assert len(environments) == 1
        assert environments[0].starts == 1
        assert (
            environments[0].config["network_policy"].network_mode.value == "no-network"
        )
    assert environments[0].stops == 1
    assert "job-end" in (tmp_path / "worker/jobs.jsonl").read_text()
    with pytest.raises(RuntimeError, match="closed"):
        await session.run("print(3)")


@pytest.mark.asyncio
async def test_timeout_destroys_worker(tmp_path):
    env = FakeEnvironment()
    env.fail = True
    async with WorkerSession(
        Worker(), directory=tmp_path / "worker", factory=lambda **kwargs: env
    ) as session:
        with pytest.raises(TimeoutError):
            await session.run("while True: pass")
        assert env.stops == 1
        assert session.environment is None
    assert env.stops == 1
