"""Studio exports inspectable definitions without running author-supplied code."""

import io
import zipfile
import asyncio
import json
from types import SimpleNamespace

import pytest

from fastapi.testclient import TestClient

from sprint.cli.env import load_experiment
from sprint.studio import create_app
from sprint.studio import Draft, render
from sprint.studio_runs import StudioRuns, tail


def test_export_compiles_private_verification_without_executing_it(tmp_path):
    client = TestClient(create_app())
    draft = {
        "name": "first-task",
        "objective": "Write 42 in answer.txt",
        "evaluation": "exact",
        "files": {"hello.py": "print('hello')"},
    }
    response = client.post("/api/export", json=draft)
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        archive.extractall(tmp_path)
    experiment = load_experiment(tmp_path / "first-task")
    compiled = experiment.compile(tmp_path / "compiled")
    assert (compiled / "tests/bundle/expected.txt").read_text() == "42"
    assert not list((compiled / "environment").rglob("expected.txt"))
    assert (tmp_path / "first-task/workspace/hello.py").is_file()
    draft.update(
        evaluation="custom",
        verifier="raise RuntimeError('do not execute')\ndef verify(context): return {'score': 0}\n",
    )
    assert client.post("/api/preview", json=draft).status_code == 200


def test_studio_rejects_unsafe_files_bad_verifiers_and_cross_origin_requests():
    client = TestClient(create_app())
    draft = {"name": "example", "objective": "test", "evaluation": "exact"}
    assert (
        client.post(
            "/api/export", json={**draft, "files": {"../escape": "x"}}
        ).status_code
        == 422
    )

    assert (
        client.post(
            "/api/preview",
            json={**draft, "evaluation": "custom", "verifier": "import os"},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/export", json=draft, headers={"Origin": "https://other.example"}
        ).status_code
        == 403
    )


@pytest.mark.asyncio
async def test_live_records_cancel_and_unknown_owner_are_retained(
    tmp_path, monkeypatch
):
    started = asyncio.Event()

    async def execute(*, directory, backend):
        directory.mkdir()
        (directory / "agent-events.jsonl").write_text(
            json.dumps({"phase": "agent", "text": "working\n"}) + "\n"
        )
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            (directory / "cleanup-errors.json").write_text(
                '[{"error":"test cleanup failed"}]'
            )

    monkeypatch.setattr(
        "sprint.studio_runs.load_experiment", lambda path: SimpleNamespace(run=execute)
    )
    runs = StudioRuns(tmp_path)
    draft = Draft(name="trial", objective="test", evaluation="exact")
    state = runs.start(draft, render(draft)["files"], "modal")
    await started.wait()
    identity = state["id"]
    source = runs.sources(identity)["agent"]
    first = tail(source, 0)
    assert first["events"][0]["text"] == "working\n"
    assert tail(source, first["cursor"])["events"] == []
    with source.open("a") as stream:
        stream.write('{"text":"partial')
    assert tail(source, first["cursor"])["cursor"] == first["cursor"]
    with source.open("a") as stream:
        stream.write('"}\n')
    assert tail(source, first["cursor"])["events"] == [{"text": "partial"}]
    assert StudioRuns(tmp_path).snapshot(identity)["status"] == "unknown"
    runs.cancel(identity)
    await runs.close()
    snapshot = runs.snapshot(identity)
    assert snapshot["status"] == "cancelled"
    assert snapshot["cleanup_errors"] == [{"error": "test cleanup failed"}]
    with pytest.raises(ValueError, match="identifier"):
        runs.root("../elsewhere")
