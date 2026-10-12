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
