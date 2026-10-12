"""Check untrusted queue handling separately from provider execution."""

import json
from types import SimpleNamespace

import pytest

from sprint import Worker
from sprint.broker import ResourceBroker
from sprint import resource_client


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
    assert not list(queue.iterdir())


@pytest.mark.asyncio
async def test_unknown_resource_returns_failure_without_provisioning(tmp_path):
    uploads = []

    async def upload(source, target):
        uploads.append(json.loads(source.read_text()))

    broker = ResourceBroker(
        SimpleNamespace(upload_file=upload),
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
