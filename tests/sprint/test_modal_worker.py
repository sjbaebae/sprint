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
