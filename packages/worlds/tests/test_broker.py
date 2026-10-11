"""Check untrusted queue handling separately from provider execution."""

import json
from types import SimpleNamespace

import pytest

from worlds import Worker
from worlds.broker import ResourceBroker
from worlds import resource_client


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
