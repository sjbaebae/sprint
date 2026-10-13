"""External-host resources attempt every cleanup and retain failed identities."""

from types import SimpleNamespace

import pytest

from sprint import Worker
import sprint.modal_worker as runtime


def test_cleanup_deletes_volume_even_when_worker_stop_fails(tmp_path, monkeypatch):
    deleted = []
    monkeypatch.setattr(runtime, "create_identity", lambda path: "ssh-ed25519 AAAA")
    monkeypatch.setattr(
        runtime.modal,
        "Volume",
        SimpleNamespace(
            from_name=lambda *args, **kwargs: SimpleNamespace(hydrate=lambda: None),
            objects=SimpleNamespace(delete=lambda name, **kwargs: deleted.append(name)),
        ),
    )

    class Session:
        def __init__(self, *args, **kwargs):
            self.environment = SimpleNamespace(
                _sandbox=SimpleNamespace(object_id="sb-test")
            )
            self.fail_close = True

        async def __aenter__(self):
            return self

        async def start(self):
            pass

        async def close(self):
            if self.fail_close:
                raise RuntimeError("provider cleanup failed")

    monkeypatch.setattr(runtime, "WorkerSession", Session)
    resource = runtime.ModalWorker(
        Worker(shell=True),
        state=tmp_path,
        connection_directory=tmp_path / "connection",
        deadline=runtime.time.time() + 60,
        trial="test",
    )
    session = resource.start()
    with pytest.raises(ExceptionGroup, match="cleanup failed"):
        resource.close()
    assert len(deleted) == 1
    assert resource.session is session
    assert resource.volume is None
    session.fail_close = False
    resource.close()
    assert resource.session is None
    resource.close()
    assert len(deleted) == 1


def test_host_recovery_attempts_all_resources_and_retries_only_failures(
    tmp_path, monkeypatch
):
    import json

    sandbox_record = tmp_path / "worker.sandbox.json"
    volume_record = tmp_path / "worker.artifact-volume.json"
    sandbox_record.write_text(
        json.dumps({"sandbox_id": "sb-test", "terminated": False})
    )
    volume_record.write_text(
        json.dumps({"volume_name": "artifact-test", "deleted": False})
    )
    deleted = []

    def fail_stop(**kwargs):
        raise RuntimeError("transient provider failure")

    sandbox = SimpleNamespace(terminate=fail_stop)
    monkeypatch.setattr(
        runtime.modal, "Sandbox", SimpleNamespace(from_id=lambda identifier: sandbox)
    )
    monkeypatch.setattr(
        runtime.modal,
        "Volume",
        SimpleNamespace(
            objects=SimpleNamespace(delete=lambda name, **kwargs: deleted.append(name))
        ),
    )
    with pytest.raises(ExceptionGroup, match="recovery failed"):
        runtime.cleanup_resources(tmp_path)
    assert json.loads(volume_record.read_text())["deleted"] is True
    assert json.loads(sandbox_record.read_text())["terminated"] is False
    sandbox.terminate = lambda **kwargs: None
    runtime.cleanup_resources(tmp_path)
    assert json.loads(sandbox_record.read_text())["terminated"] is True
    assert deleted == ["artifact-test"]
