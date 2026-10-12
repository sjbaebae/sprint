"""Check untrusted queue handling separately from provider execution."""

import json
from types import SimpleNamespace

import pytest

from sprint import Worker
from sprint.broker import ResourceBroker
from sprint import resource_client


async def successful_exec(*args, **kwargs):
    return SimpleNamespace(return_code=0, stdout="", stderr="")


def test_queue_rejects_links_special_files_and_malformed_json(tmp_path, monkeypatch):
    monkeypatch.setattr(resource_client, "ROOT", tmp_path)
    queue = tmp_path / "requests"
    queue.mkdir()
    (queue / ("a" * 32 + ".json")).write_text('{"resource":"train","code":"print(1)"}')
    (queue / ("b" * 32 + ".json")).symlink_to("/etc/passwd")
    (queue / ("c" * 32 + ".json")).write_text("malformed")
    result = resource_client.read_requests()
    assert len(result) == 1
    assert result[0]["request_id"] == "a" * 32
    assert [path.name for path in queue.iterdir()] == ["a" * 32 + ".json"]


@pytest.mark.asyncio
async def test_unknown_resource_returns_failure_without_provisioning(tmp_path):
    uploads = []

    async def upload(source, target):
        uploads.append(json.loads(source.read_text()))

    broker = ResourceBroker(
        SimpleNamespace(upload_file=upload, exec=successful_exec),
        {"train": Worker()},
        directory=tmp_path,
        backend="docker",
    )
    await broker.dispatch(
        {"request_id": "a" * 32, "payload": {"resource": "other", "code": "print(1)"}}
    )
    assert not broker.sessions
    assert uploads[0]["status"] == "error"
    assert uploads[0]["message"] == "resource is not granted to this trial"
    await broker.dispatch({"request_id": "a" * 32, "payload": {}})
    assert len(uploads) == 1


def test_spending_reserves_full_lifetime_and_refuses_unknown_rates(tmp_path):
    from sprint import Budget

    broker = ResourceBroker(
        object(),
        {"a": Worker(hourly_cost=2), "b": Worker(hourly_cost=2), "unknown": Worker()},
        directory=tmp_path,
        backend="modal",
        budget=Budget(worker_spend=3),
    )
    broker.reserve("a")
    broker.reserve("a")
    assert broker.reservations == {"a": 2}
    with pytest.raises(ValueError, match="exceeds"):
        broker.reserve("b")
    with pytest.raises(ValueError, match="hourly_cost"):
        broker.reserve("unknown")
    assert json.loads((tmp_path / "spending.json").read_text())["reservations"] == {
        "a": 2
    }


@pytest.mark.asyncio
async def test_concurrent_batch_respects_slots(tmp_path):
    import asyncio
    from sprint import Budget

    broker = ResourceBroker(
        object(),
        {},
        directory=tmp_path,
        backend="modal",
        budget=Budget(worker_concurrency=2),
    )
    active = peak = 0
    ready = asyncio.Event()

    async def dispatch(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 2:
            ready.set()
        await asyncio.wait_for(ready.wait(), 1)
        await asyncio.sleep(0)
        active -= 1

    broker.dispatch = dispatch
    async with asyncio.TaskGroup() as group:
        for _ in range(5):
            group.create_task(broker.bounded_dispatch({}))
    assert peak == 2


def test_invalid_filenames_cannot_starve_valid_queue_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(resource_client, "ROOT", tmp_path)
    queue = tmp_path / "requests"
    queue.mkdir()
    for index in range(40):
        (queue / f"000-invalid-{index}").write_text("junk")
    (queue / ("f" * 32 + ".json")).write_text('{"resource":"train","code":"print(1)"}')
    assert len(resource_client.read_requests()) == 1


@pytest.mark.asyncio
async def test_restore_keeps_failed_workers_closed_and_preserves_reservations(
    tmp_path, monkeypatch
):
    from sprint.backends.runtime import restore_workers
    from sprint.workers import WorkerSession

    async def forbidden_start(self):
        raise RuntimeError("closed worker must not be provisioned during restore")

    monkeypatch.setattr(WorkerSession, "start", forbidden_start)
    broker = ResourceBroker(
        object(),
        {"train": Worker(hourly_cost=2)},
        directory=tmp_path / "broker",
        backend="modal",
        contexts={"train": tmp_path / "context"},
    )
    checkpoint = SimpleNamespace(
        metadata={
            "broker": {
                "seen": ["a" * 32],
                "completed": {},
                "reservations": {"train": 2},
            },
            "workers": {"train": {"closed": True}},
        }
    )
    await restore_workers(broker, checkpoint, tmp_path / "missing-records")
    assert broker.reservations == {"train": 2}
    assert broker.sessions["train"].closed
    with pytest.raises(RuntimeError, match="closed"):
        await broker.sessions["train"].run("print(1)")


@pytest.mark.asyncio
async def test_feedback_retry_is_atomic_and_never_reruns_training(
    tmp_path, monkeypatch
):
    import asyncio

    uploads, commands, executions = [], [], []

    async def upload(source, target):
        uploads.append(target)
        assert target.endswith(".tmp")
        if len(uploads) == 1:
            raise OSError("transient storage failure")
        assert json.loads(source.read_text())["stdout"] == "trained once"

    async def execute(command, **kwargs):
        commands.append(command)
        assert kwargs["user"] == "root"
        assert "chmod 444" in command and "mv -f --" in command
        return SimpleNamespace(return_code=0, stdout="", stderr="")

    async def no_sleep(*args):
        pass

    async def run(code):
        executions.append(code)
        return {"return_code": 0, "stdout": "trained once", "stderr": ""}

    broker = ResourceBroker(
        SimpleNamespace(upload_file=upload, exec=execute),
        {"train": Worker()},
        directory=tmp_path,
        backend="modal",
    )

    async def session(name):
        return SimpleNamespace(run=run)

    monkeypatch.setattr(broker, "session", session)
    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    await broker.dispatch(
        {"request_id": "a" * 32, "payload": {"resource": "train", "code": "train()"}}
    )
    assert executions == ["train()"] and len(uploads) == 2 and len(commands) == 1
    failure = json.loads((tmp_path / "delivery.jsonl").read_text())
    assert failure["attempt"] == 1 and "transient storage" in failure["message"]
    assert broker.completed == {"a" * 32: "train"}


@pytest.mark.asyncio
async def test_exhausted_feedback_retries_retain_operator_result(tmp_path, monkeypatch):
    import asyncio

    async def upload(*args):
        raise OSError("storage unavailable")

    async def no_sleep(*args):
        pass

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    broker = ResourceBroker(
        SimpleNamespace(upload_file=upload), {}, directory=tmp_path, backend="modal"
    )
    with pytest.raises(RuntimeError, match="after 3 attempts"):
        await broker.dispatch(
            {
                "request_id": "a" * 32,
                "payload": {"resource": "missing", "code": "train()"},
            }
        )
    assert (
        json.loads((tmp_path / ("a" * 32 + ".json")).read_text())["status"] == "error"
    )
    assert len((tmp_path / "delivery.jsonl").read_text().splitlines()) == 3


@pytest.mark.asyncio
async def test_final_cleanup_retries_retained_handles_and_records_every_failure(
    tmp_path,
):
    from sprint.broker import finalize_brokers

    attempted = []

    class Broker:
        def __init__(self, name, fail):
            self.name, self.fail = name, fail

        async def close(self):
            attempted.append(self.name)
            if self.fail:
                raise TimeoutError("provider deletion pending")

    await finalize_brokers(
        {"bad": Broker("bad", True), "good": Broker("good", False)}, tmp_path
    )
    assert set(attempted) == {"bad", "good"}
    errors = json.loads((tmp_path / "cleanup-errors.json").read_text())
    assert len(errors) == 1
    assert errors[0]["trial"] == "bad"
    assert errors[0]["error_type"] == "TimeoutError"
    assert errors[0]["timestamp"]


@pytest.mark.asyncio
async def test_broker_consumes_service_failure_but_preserves_failed_cleanup_handle(
    tmp_path,
):
    import asyncio
    from sprint.broker import finalize_brokers

    broker = ResourceBroker(object(), {}, directory=tmp_path, backend="modal")

    async def failure():
        raise RuntimeError("service stopped")

    class Session:
        closed = False
        environment = object()
        attempts = 0

        async def close(self):
            self.attempts += 1
            if self.attempts == 1:
                raise TimeoutError("first teardown failed")
            self.environment = None

    session = Session()
    broker.sessions["train"] = session
    broker.task = asyncio.create_task(failure())
    await asyncio.sleep(0)
    with pytest.raises(ExceptionGroup) as error:
        await broker.close()
    assert "service stopped" in str(error.value.exceptions[0])
    assert "first teardown failed" in str(error.value.exceptions[1].exceptions[0])
    assert broker.task is None
    assert session.environment is not None
    await finalize_brokers({"trial": broker}, tmp_path)
    assert session.attempts == 2
    assert session.environment is None
    assert not (tmp_path / "cleanup-errors.json").exists()


def test_queue_read_is_retryable_until_feedback_is_published(tmp_path, monkeypatch):
    monkeypatch.setattr(resource_client, "ROOT", tmp_path)
    queue = tmp_path / "requests"
    queue.mkdir()
    results = tmp_path / "results"
    results.mkdir()
    name = "d" * 32 + ".json"
    request = queue / name
    request.write_text('{"resource":"train","code":"print(7)"}')
    first = resource_client.read_requests()
    assert request.exists()
    # Model a response lost between the container and the operator host.
    assert resource_client.read_requests() == first
    staging = results / ("." + name + ".tmp")
    staging.write_text('{"status":"completed"}')
    assert resource_client.read_requests() == first
    staging.rename(results / name)
    assert resource_client.read_requests() == []
    assert not request.exists()


def test_acknowledged_batch_does_not_permanently_starve_next_requests(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(resource_client, "ROOT", tmp_path)
    queue = tmp_path / "requests"
    queue.mkdir()
    results = tmp_path / "results"
    results.mkdir()
    for index in range(33):
        name = f"{index:032x}.json"
        (queue / name).write_text("{}")
        if index < 32:
            (results / name).write_text('{"status":"error"}')
    assert resource_client.read_requests() == []
    remaining = resource_client.read_requests()
    assert len(remaining) == 1
    assert remaining[0]["request_id"] == f"{32:032x}"


@pytest.mark.asyncio
async def test_lost_queue_read_response_is_retried_without_dispatch(
    tmp_path, monkeypatch
):
    import asyncio

    attempts = 0
    request = {
        "request_id": "a" * 32,
        "payload": {"resource": "train", "code": "print(1)"},
    }

    async def execute(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionError("read response lost")
        return SimpleNamespace(return_code=0, stdout=json.dumps([request]), stderr="")

    async def no_delay(_):
        pass

    monkeypatch.setattr(asyncio, "sleep", no_delay)
    broker = ResourceBroker(
        SimpleNamespace(exec=execute), {}, directory=tmp_path, backend="modal"
    )
    assert await broker.read_queue() == [request]
    assert attempts == 2
    assert not broker.seen
    errors = [
        json.loads(line) for line in (tmp_path / "queue.jsonl").read_text().splitlines()
    ]
    assert errors[0]["error_type"] == "ConnectionError"
    assert errors[0]["attempt"] == 1


@pytest.mark.asyncio
async def test_queue_read_failure_is_bounded_and_keeps_original_cause(
    tmp_path, monkeypatch
):
    import asyncio

    async def execute(*args, **kwargs):
        raise ConnectionError("provider unreachable")

    async def no_delay(_):
        pass

    monkeypatch.setattr(asyncio, "sleep", no_delay)
    broker = ResourceBroker(
        SimpleNamespace(exec=execute), {}, directory=tmp_path, backend="modal"
    )
    with pytest.raises(RuntimeError, match="after 3 attempts") as error:
        await broker.read_queue()
    assert isinstance(error.value.__cause__, ConnectionError)
    assert len((tmp_path / "queue.jsonl").read_text().splitlines()) == 3
