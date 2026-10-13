"""Worker ownership and checkpoint reuse without provider credentials."""

from types import SimpleNamespace
import json
import hashlib

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
        Worker(),
        directory=tmp_path / "worker",
        factory=factory,
        session_id="0123456789abcdef0123456789abcdef",
    ) as session:
        first = await session.run("print(1)")
        second = await session.run("print(2)")
        assert first["job_id"] != second["job_id"]
        assert len(environments) == 1
        assert environments[0].starts == 1
        assert (
            environments[0].config["session_id"] == "0123456789abcdef0123456789abcdef"
        )
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


@pytest.mark.asyncio
async def test_shell_reuses_worker_and_cleans_up_on_timeout(tmp_path):
    class ShellEnvironment(FakeEnvironment):
        async def exec(self, command, **kwargs):
            assert kwargs["user"] == "agent"
            assert kwargs["cwd"] == "/app"
            if self.fail:
                raise TimeoutError("command timeout")
            return SimpleNamespace(
                return_code=124 if "sleep 2" in command else 0,
                stdout="running",
                stderr="",
            )

    denied = WorkerSession(Worker(), directory=tmp_path / "denied")
    with pytest.raises(PermissionError):
        await denied.shell("echo forbidden")
    env = ShellEnvironment()
    async with WorkerSession(
        Worker(shell=True), directory=tmp_path / "shell", factory=lambda **kwargs: env
    ) as session:
        first = await session.shell("sleep 60 > /app/process.log 2>&1 &")
        second = await session.shell("cat /app/process.log")
        assert first["job_id"] != second["job_id"]
        assert env.starts == 1 and env.stops == 0
        expired = await session.shell("sleep 2", seconds=1)
        assert expired["return_code"] == 124
        assert not session.closed and env.stops == 0
        env.fail = True
        with pytest.raises(TimeoutError):
            await session.shell("sleep 999")
        assert env.stops == 1


@pytest.mark.asyncio
async def test_upload_is_bounded_and_writes_as_learner(tmp_path):
    calls = []
    env = FakeEnvironment()

    async def execute(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(return_code=0, stdout="", stderr="")

    env.exec = execute
    async with WorkerSession(
        Worker(), directory=tmp_path / "upload", factory=lambda **kwargs: env
    ) as session:
        for path in ("../escape", "/etc/config"):
            with pytest.raises(ValueError):
                await session.upload(b"code", path)
        with pytest.raises(ValueError):
            await session.upload(b"x" * (1024 * 1024 + 1), "large")
        assert env.starts == 0
        result = await session.upload(b"print(42)", "code/train.py")
        assert result["path"] == "/app/code/train.py" and result["bytes"] == 9
        writes = [
            (command, options)
            for command, options in calls
            if "python -I -S" in command
        ]
        assert len(writes) == 1 and writes[0][1]["user"] == "agent"
        assert "target.relative_to(root)" in writes[0][0]
        assert not list(session.directory.glob("*.upload"))


@pytest.mark.asyncio
async def test_read_only_volume_grant_reaches_modal_provider(tmp_path):
    worker = Worker(read_only_volumes={"/base": "pinned-release"})
    with pytest.raises(ValueError, match="Modal"):
        WorkerSession(worker, directory=tmp_path / "unsupported", backend="docker")
    with pytest.raises(ValueError, match="dedicated"):
        Worker(read_only_volumes={"/app": "pinned-release"})
    env = FakeEnvironment()
    configs = []

    def factory(**kwargs):
        configs.append(kwargs["config"])
        return env

    async with WorkerSession(
        worker, directory=tmp_path / "modal", backend="modal", factory=factory
    ) as session:
        await session.start()
        assert configs[0].kwargs["read_only_volumes"] == {"/base": "pinned-release"}


@pytest.mark.asyncio
async def test_operator_file_upload_snapshots_large_input_and_enforces_limit(tmp_path):
    env = FakeEnvironment()

    async def execute(*args, **kwargs):
        return SimpleNamespace(return_code=0, stdout="", stderr="")

    env.exec = execute
    source = tmp_path / "dataset.jsonl"
    content = b"data\n" * 220000
    source.write_bytes(content)
    async with WorkerSession(
        Worker(), directory=tmp_path / "worker", factory=lambda **kwargs: env
    ) as session:
        with pytest.raises(ValueError, match="bounded"):
            await session.upload_file(source, "data/train.jsonl", max_bytes=100)
        assert env.starts == 0
        result = await session.upload_file(source, "data/train.jsonl")
        assert result["bytes"] == len(content)
        assert result["sha256"] == hashlib.sha256(content).hexdigest()
        assert source.read_bytes() == content
        assert not list(session.directory.glob("*.upload"))


@pytest.mark.asyncio
@pytest.mark.parametrize("commit_fails", [False, True])
async def test_private_volume_export_commits_before_issuing_receipt(
    tmp_path, commit_fails
):
    env = FakeEnvironment()
    calls = []

    async def execute(command, **options):
        calls.append((command, options))
        if "--volume" in command:
            return SimpleNamespace(
                return_code=0,
                stderr="",
                stdout=json.dumps(
                    {
                        "path": "/opt/sprint-artifacts/sprint-export-test",
                        "bytes": 10,
                        "sha256": "a" * 64,
                    }
                ),
            )
        return SimpleNamespace(
            return_code=int(commit_fails and command.startswith("sync")),
            stdout="",
            stderr="",
        )

    env.exec = execute
    async with WorkerSession(
        Worker(backend="modal"),
        directory=tmp_path / "worker",
        artifact_volume="test-artifacts",
        factory=lambda **kwargs: env,
    ) as session:
        await session.start()
        assert "chmod 700 /opt/sprint-artifacts" in calls[0][0]
        if commit_fails:
            with pytest.raises(RuntimeError, match="commit failed"):
                await session.collect_to_volume("checkpoint")
            assert session.closed
        else:
            receipt = await session.collect_to_volume("checkpoint")
            assert receipt["path"] == "sprint-export-test"
            assert receipt["volume"] == "test-artifacts"
            assert calls[-1][0] == "sync /opt/sprint-artifacts"
        assert all(options["user"] == "root" for _, options in calls)


@pytest.mark.asyncio
async def test_direct_start_timeout_cleans_up_provider(tmp_path):
    import asyncio
    import time

    env = FakeEnvironment()

    async def stalled_start(**kwargs):
        await asyncio.Event().wait()

    env.start = stalled_start
    async with WorkerSession(
        Worker(), directory=tmp_path / "worker", factory=lambda **kwargs: env
    ) as session:
        session.deadline = time.monotonic() + 0.05
        with pytest.raises(TimeoutError):
            await session.start()
        assert session.closed and env.stops == 1
