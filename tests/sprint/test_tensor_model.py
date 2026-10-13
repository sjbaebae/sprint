"""Custom inference receives only a package and input tensors, never grading mounts."""

from types import SimpleNamespace

import pytest

import sprint.tensor_model as inference_session


@pytest.mark.asyncio
async def test_model_worker_is_network_blocked_and_outputs_are_checked(
    tmp_path, monkeypatch
):
    workers = []
    uploaded = []

    class Session:
        def __init__(self, worker, *, directory):
            workers.append(self)
            self.worker = worker
            self.directory = directory
            self.closed = False
            self.output = "[[1,2,3,4]]"
            self.exited = False
            self.environment = SimpleNamespace(
                upload_file=self.upload, exec=self.execute
            )

        async def __aenter__(self):
            self.directory.mkdir()

        async def start(self):
            pass

        async def upload(self, source, target):
            uploaded.append(target)

        async def execute(self, command, **kwargs):
            if command.startswith("if test -f") and self.exited:
                return SimpleNamespace(return_code=3, stdout="")
            return SimpleNamespace(
                return_code=0, stdout=self.output if command.startswith("head ") else ""
            )

        async def close(self):
            self.closed = True

    monkeypatch.setattr(inference_session, "WorkerSession", Session)
    package = tmp_path / "model.tar"
    package.write_bytes(b"synthetic package")
    async with inference_session.TensorModelSession(
        package, directory=tmp_path / "worker", vocabulary_size=7, seconds=60
    ) as service:
        assert workers[0].worker.sandbox.internet == "none"
        assert workers[0].worker.read_only_volumes == {}
        assert await service.predict([[1, 2]], [0, 1, 2, 3]) == [[1, 2, 3, 4]]
        workers[0].output = '[[1,2,3,"not a tensor"]]'
        with pytest.raises(ValueError, match="output tensor"):
            await service.predict([[1, 2]], [0, 1, 2, 3])
        workers[0].exited = True
        with pytest.raises(RuntimeError, match="exited before responding"):
            await service.predict([[1, 2]], [0, 1, 2, 3])
    assert workers[0].closed
    assert uploaded[:3] == [
        "/tmp/model.tar",
        "/tmp/tensor_model_worker.py",
        "/tmp/tensor_model_package.py",
    ]


@pytest.mark.asyncio
async def test_stalled_package_transfer_expires_and_cleans_up(tmp_path, monkeypatch):
    import asyncio

    closed = []

    class Session:
        def __init__(self, *args, **kwargs):
            self.closed = False
            self.environment = SimpleNamespace(upload_file=self.upload)

        async def __aenter__(self):
            return self

        async def start(self):
            pass

        async def upload(self, *args):
            await asyncio.Event().wait()

        async def close(self):
            self.closed = True
            closed.append(True)

    monkeypatch.setattr(inference_session, "WorkerSession", Session)
    with pytest.raises(TimeoutError):
        async with inference_session.TensorModelSession(
            tmp_path / "model.tar",
            directory=tmp_path / "worker",
            vocabulary_size=7,
            seconds=1,
        ):
            pytest.fail("a stalled transfer must not expose a ready session")
    assert closed == [True]
