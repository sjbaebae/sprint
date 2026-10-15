"""Software execution shares worker ownership without sharing private scoring."""

import asyncio

import pytest

import sprint.software as software
from sprint import Sandbox


@pytest.mark.asyncio
async def test_software_boundary_and_cleanup(tmp_path, monkeypatch):
    submission = tmp_path / "submission"
    submission.mkdir()
    (submission / "main.py").write_text("print('candidate')")
    uploaded, closed = [], []

    class Session:
        def __init__(self, spec, **kwargs):
            assert spec.sandbox.internet == "none"
            assert not spec.read_only_volumes
            assert not spec.shell
            self.closed = False

        async def __aenter__(self):
            return self

        async def start(self):
            pass

        async def upload_file(self, source, target):
            uploaded.append(target)

        async def upload(self, content, target):
            uploaded.append(content)

        async def run(self, source):
            assert "os.execvpe" in source
            assert "os.dup2" in source
            return {"return_code": 7, "stdout": "", "stderr": "failed"}

        async def close(self):
            self.closed = True
            closed.append(True)

    monkeypatch.setattr(software, "WorkerSession", Session)
    async with software.SoftwareSession(
        submission, directory=tmp_path / "records"
    ) as session:
        result = await session.run(["python", "main.py"], stdin=b"public input")
        assert result["return_code"] == 7
    assert uploaded == ["submission/main.py", b"public input"]
    assert closed == [True]
    (submission / "escape").symlink_to(tmp_path)
    with pytest.raises(ValueError, match="link"):
        async with software.SoftwareSession(submission, directory=tmp_path / "bad"):
            pytest.fail("unsafe submission was accepted")
    with pytest.raises(ValueError, match="offline"):
        software.SoftwareSession(
            submission, directory=tmp_path / "bad", sandbox=Sandbox(internet="full")
        )


@pytest.mark.asyncio
async def test_software_startup_cancellation_closes_worker(tmp_path, monkeypatch):
    submission = tmp_path / "submission"
    submission.mkdir()
    (submission / "main.py").write_text("pass")
    closed = []

    class Session:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def start(self):
            raise asyncio.CancelledError

        async def close(self):
            closed.append(True)

    monkeypatch.setattr(software, "WorkerSession", Session)
    with pytest.raises(asyncio.CancelledError):
        async with software.SoftwareSession(submission, directory=tmp_path / "records"):
            pytest.fail("cancelled startup returned a session")
    assert closed == [True]
