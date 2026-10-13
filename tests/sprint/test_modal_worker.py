"""External-host resources attempt every cleanup and retain failed identities."""

from types import SimpleNamespace

import pytest

from sprint import Worker
import sprint.modal_worker as runtime


def test_cleanup_deletes_volume_even_when_worker_stop_fails(tmp_path, monkeypatch):
    deleted = []

    async def hydrate():
        pass

    monkeypatch.setattr(runtime, "create_identity", lambda path: "ssh-ed25519 AAAA")
    monkeypatch.setattr(
        runtime.modal,
        "Volume",
        SimpleNamespace(
            from_name=lambda *args, **kwargs: SimpleNamespace(
                hydrate=SimpleNamespace(aio=hydrate)
            ),
            objects=SimpleNamespace(delete=lambda name, **kwargs: deleted.append(name)),
        ),
    )

    class Session:
        def __init__(self, worker, **kwargs):
            assert worker.lifetime_seconds == 40
            self.session_id = "0123456789abcdef0123456789abcdef"
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
        Worker(shell=True, lifetime_seconds=40),
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


def test_recovery_cancels_creator_before_finding_unrecorded_sandbox(
    tmp_path, monkeypatch
):
    import json

    operations = []
    (tmp_path / "evaluation.call.json").write_text(
        json.dumps({"call_id": "fc-test", "terminate_containers": True})
    )
    (tmp_path / "evaluation.sandbox.json").write_text(
        json.dumps({"session_id": "session-test", "terminated": False})
    )

    def failed_cancel(**kwargs):
        operations.append(("cancel", kwargs))
        raise RuntimeError("creator cancellation failed")

    call = SimpleNamespace(
        cancel=failed_cancel,
        get_call_graph=lambda: [
            SimpleNamespace(function_call_id="fc-child", children=[])
        ],
    )
    child = SimpleNamespace(
        cancel=lambda **kwargs: operations.append(("cancel-child", kwargs)),
        get_call_graph=lambda: [],
    )
    sandbox = SimpleNamespace(
        terminate=lambda **kwargs: operations.append(("terminate", kwargs))
    )

    def discover(*, app_id, tags):
        assert app_id == "ap-workers"
        assert tags == {"sprint.managed": "true", "sprint.session_id": "session-test"}
        return [sandbox]

    monkeypatch.setattr(
        runtime.modal,
        "FunctionCall",
        SimpleNamespace(
            from_id=lambda identity: call if identity == "fc-test" else child
        ),
    )
    monkeypatch.setattr(runtime.modal, "Sandbox", SimpleNamespace(list=discover))
    monkeypatch.setattr(
        runtime.modal,
        "App",
        SimpleNamespace(lookup=lambda name: SimpleNamespace(app_id="ap-workers")),
    )
    with pytest.raises(ExceptionGroup, match="recovery failed"):
        runtime.cleanup_resources(tmp_path)
    record = json.loads((tmp_path / "evaluation.sandbox.json").read_text())
    assert record["terminated"] is False
    call.cancel = lambda **kwargs: operations.append(("cancel", kwargs))
    runtime.cleanup_resources(tmp_path)
    assert operations == [
        ("cancel", {"terminate_containers": True}),
        ("terminate", {"wait": True}),
        ("cancel", {"terminate_containers": True}),
        ("cancel-child", {"terminate_containers": True}),
        ("terminate", {"wait": True}),
    ]
    runtime.cleanup_resources(tmp_path)
    assert len(operations) == 5
