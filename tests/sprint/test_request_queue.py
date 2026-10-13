"""One-worker admission, safe feedback and terminal records."""

import json
import time

from sprint.request_queue import LocalRequestQueue


def test_queue_serializes_requests_and_retains_private_errors(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    calls = []

    def prepare(request, request_id):
        if request["kind"] == "submit":
            return lambda: {"submitted": True}

        def run():
            calls.append(request_id)
            time.sleep(0.02)
            return {"value": 1}

        return run

    queue = LocalRequestQueue(
        tmp_path / "rpc",
        state,
        deadline=time.time() + 0.15,
        prepare=prepare,
        completed=lambda result: None,
        failure_feedback=lambda error, stage: {
            "failure_stage": stage,
            "message": "redacted",
        },
        poll_seconds=0.001,
        inline_kinds=("submit",),
    )
    ids = [f"{index:032x}" for index in range(1, 4)]
    for request_id, kind in zip(ids, ["run", "run", "submit"]):
        (queue.rpc / "requests" / (request_id + ".json")).write_text(
            json.dumps({"kind": kind})
        )
    queue.run()
    responses = [
        json.loads((queue.rpc / "responses" / (request_id + ".json")).read_text())
        for request_id in ids
    ]
    assert responses[0] == {"status": "complete", "result": {"value": 1}}
    assert responses[1]["failure_stage"] == "busy"
    assert responses[1]["active_request_id"] == ids[0]
    assert responses[2] == {"status": "complete", "result": {"submitted": True}}
    assert calls == [ids[0]]


def test_queue_redacts_validation_errors(tmp_path):
    state = tmp_path / "state"
    state.mkdir()

    def reject(request, request_id):
        raise ValueError("private diagnostic")

    queue = LocalRequestQueue(
        tmp_path / "rpc",
        state,
        deadline=time.time() + 0.02,
        prepare=reject,
        completed=lambda result: None,
        failure_feedback=lambda error, stage: {
            "failure_stage": stage,
            "message": "redacted",
        },
        poll_seconds=0.001,
    )
    request_id = "0" * 32
    (queue.rpc / "requests" / (request_id + ".json")).write_text("{}")
    queue.run()
    response = (queue.rpc / "responses" / (request_id + ".json")).read_text()
    assert "private diagnostic" not in response
    assert "private diagnostic" in (state / (request_id + ".error.txt")).read_text()
