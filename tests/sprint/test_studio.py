"""Studio exports inspectable definitions without running author-supplied code."""

import io
import zipfile

from fastapi.testclient import TestClient

from sprint.cli.env import load_experiment
from sprint.studio import create_app


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
