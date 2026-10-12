"""Worker ownership and checkpoint reuse without provider credentials."""

from types import SimpleNamespace
import json

import pytest

from sprint import Worker
from sprint.workers import WorkerSession


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
        assert kwargs["user"] == "root"
        assert type(kwargs["timeout_sec"]) is int
        if self.fail:
            raise TimeoutError("deadline exceeded")
        return SimpleNamespace(
            return_code=0,
            stdout=json.dumps(
                {"return_code": 0, "stdout": "ok", "stderr": "", "timed_out": False}
            ),
            stderr="",
        )


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


@pytest.mark.asyncio
async def test_worker_lifetime_expires_without_provisioning(tmp_path):
    async with WorkerSession(
        Worker(),
        directory=tmp_path / "worker",
        factory=lambda **kw: (_ for _ in ()).throw(RuntimeError("must not provision")),
    ) as session:
        session.deadline = 0
        with pytest.raises(TimeoutError, match="lifetime"):
            await session.run("print(1)")


@pytest.mark.asyncio
async def test_cleanup_attempts_all_resources_when_one_provider_fails():
    from sprint.workers import close_all

    called = []

    class Resource:
        def __init__(self, name, fail=False):
            self.name, self.fail = name, fail

        async def close(self):
            called.append(self.name)
            if self.fail:
                raise RuntimeError("provider teardown failed")

    with pytest.raises(ExceptionGroup, match="cleanup failed"):
        await close_all([Resource("bad", True), Resource("good")])
    assert sorted(called) == ["bad", "good"]


@pytest.mark.asyncio
async def test_failed_worker_cannot_reprovision_on_existing_reservation(tmp_path):
    env = FakeEnvironment()
    env.fail = True
    async with WorkerSession(
        Worker(), directory=tmp_path / "worker", factory=lambda **kwargs: env
    ) as session:
        with pytest.raises(TimeoutError):
            await session.run("print(1)")
        env.fail = False
        with pytest.raises(RuntimeError, match="closed"):
            await session.run("print(2)")
        assert env.starts == 1


@pytest.mark.asyncio
async def test_stalled_upload_is_bounded_by_lifetime(tmp_path):
    import asyncio
    import time

    env = FakeEnvironment()

    async def upload(*args):
        await asyncio.Event().wait()

    env.upload_file = upload
    async with WorkerSession(
        Worker(), directory=tmp_path / "worker", factory=lambda **kwargs: env
    ) as session:
        started = time.monotonic()
        session.deadline = started + 0.05
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(session.run("print(1)"), timeout=3)
        assert time.monotonic() - started < 1
        assert session.closed and env.stops == 1


@pytest.mark.asyncio
async def test_expired_collection_destroys_worker(tmp_path):
    env = FakeEnvironment()
    async with WorkerSession(
        Worker(), directory=tmp_path / "worker", factory=lambda **kwargs: env
    ) as session:
        await session.run("print(1)")
        session.deadline = 0
        with pytest.raises(TimeoutError, match="lifetime"):
            await session.collect("checkpoint", tmp_path / "artifact")
        assert session.closed and env.stops == 1


@pytest.mark.asyncio
async def test_hung_cleanup_is_bounded_and_remains_retryable(tmp_path, monkeypatch):
    import asyncio
    import sprint.workers as workers

    env = FakeEnvironment()
    session = WorkerSession(
        Worker(), directory=tmp_path / "worker", factory=lambda **kwargs: env
    )
    await session.__aenter__()
    await session.run("print(1)")
    original_stop = env.stop

    async def hung_stop(**kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(workers, "CLEANUP_SECONDS", 0.02)
    env.stop = hung_stop
    with pytest.raises(TimeoutError):
        await session.close()
    assert session.closed and session.environment is env
    assert "worker-cleanup-error" in (session.directory / "jobs.jsonl").read_text()
    env.stop = original_stop
    await session.close()
    assert session.environment is None and env.stops == 1


@pytest.mark.asyncio
async def test_timed_out_job_retains_partial_output_and_closes_worker(tmp_path):
    env = FakeEnvironment()

    async def timeout_feedback(command, **kwargs):
        return SimpleNamespace(
            return_code=0,
            stdout=json.dumps(
                {
                    "return_code": 124,
                    "stdout": "step 1 complete",
                    "stderr": "last diagnostic",
                    "timed_out": True,
                }
            ),
            stderr="",
        )

    env.exec = timeout_feedback
    async with WorkerSession(
        Worker(), directory=tmp_path / "worker", factory=lambda **kwargs: env
    ) as session:
        result = await session.run("while True: pass")
        assert result["timed_out"] and result["return_code"] == 124
        assert (
            result["stdout"] == "step 1 complete"
            and result["stderr"] == "last diagnostic"
        )
        assert session.closed and env.stops == 1
