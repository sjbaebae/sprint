"""One-worker admission, safe feedback and terminal records."""

import json
import time

import pytest

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
        queue.stop()
        raise ValueError("private diagnostic")

    queue = LocalRequestQueue(
        tmp_path / "rpc",
        state,
        deadline=time.time() + 3,
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


def test_resume_restores_results_without_replaying_interrupted_calls(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    executed, restored = [], []

    def prepare(request, request_id):
        def execute():
            executed.append(request_id)
            queue.stop()
            return {"value": 7}

        return execute

    settings = {
        "deadline": time.time() + 10,
        "prepare": prepare,
        "completed": restored.append,
        "failure_feedback": lambda error, stage: {"failure_stage": stage},
        "poll_seconds": 0.001,
    }
    queue = LocalRequestQueue(tmp_path / "rpc", state, **settings)
    complete, interrupted, new = [f"{index:032x}" for index in range(1, 4)]
    (queue.rpc / "requests" / (complete + ".json")).write_text('{"kind":"run"}')
    queue.run()
    restored.clear()
    with pytest.raises(ValueError, match="explicit resume"):
        LocalRequestQueue(tmp_path / "rpc", state, **settings)
    # Simulate a lost response after the durable result, and a separate call
    # interrupted after admission. Learner-visible responses are not authority.
    (state / (complete + ".feedback.json")).unlink()
    (state / (interrupted + ".request.json")).write_text('{"kind":"run"}')
    (queue.rpc / "requests" / (interrupted + ".json")).write_text('{"kind":"run"}')
    (queue.rpc / "responses" / (interrupted + ".json")).write_text(
        '{"status":"complete","result":{"value":999}}'
    )
    queue = LocalRequestQueue(tmp_path / "rpc", state, resume=True, **settings)
    assert restored == [{"value": 7}]
    feedback = json.loads(
        (queue.rpc / "responses" / (interrupted + ".json")).read_text()
    )
    assert feedback["failure_stage"] == "recovery"
    assert feedback["outcome_known"] is False
    (queue.rpc / "requests" / (new + ".json")).write_text('{"kind":"run"}')
    queue.run()
    assert executed == [complete, new]


def test_independent_resources_progress_while_same_resource_stays_serial(tmp_path):
    import threading

    state = tmp_path / "state"
    state.mkdir()
    connected = threading.Event()

    def prepare(request, request_id):
        def execute():
            if request["kind"] == "evaluate":
                if not connected.wait(timeout=1):
                    raise RuntimeError("GPU connection was blocked by evaluation")
            else:
                connected.set()
            return {"kind": request["kind"]}

        return execute

    queue = LocalRequestQueue(
        tmp_path / "rpc",
        state,
        deadline=time.time() + 0.1,
        prepare=prepare,
        completed=lambda result: None,
        failure_feedback=lambda error, stage: {"failure_stage": stage},
        groups={"evaluate": "evaluation", "connect": "gpu", "collect": "gpu"},
        poll_seconds=0.001,
    )
    ids = [f"{index:032x}" for index in range(1, 4)]
    for request_id, kind in zip(ids, ("evaluate", "connect", "collect")):
        (queue.rpc / "requests" / (request_id + ".json")).write_text(
            json.dumps({"kind": kind})
        )
    queue.run()
    results = [
        json.loads((queue.rpc / "responses" / (request_id + ".json")).read_text())
        for request_id in ids
    ]
    assert [value["status"] for value in results] == [
        "complete",
        "complete",
        "rejected",
    ]
    assert results[2]["resource_group"] == "gpu"
    assert results[2]["active_request_id"] == ids[1]


def test_stop_drains_admitted_work_without_accepting_more(tmp_path):
    import threading

    state = tmp_path / "state"
    state.mkdir()
    stopping = threading.Event()

    def prepare(request, request_id):
        if request["kind"] == "stop":

            def stop():
                queue.stop()
                stopping.set()
                return {"stopped": True}

            return stop

        def execute():
            if not stopping.wait(timeout=1):
                raise RuntimeError("shutdown was not received")
            return {"finished": True}

        return execute

    queue = LocalRequestQueue(
        tmp_path / "rpc",
        state,
        deadline=time.time() + 60,
        prepare=prepare,
        completed=lambda result: None,
        failure_feedback=lambda error, stage: {"failure_stage": stage},
        inline_kinds=("stop",),
        poll_seconds=0.001,
    )
    ids = [f"{index:032x}" for index in range(1, 4)]
    for request_id, kind in zip(ids, ("run", "stop", "run")):
        (queue.rpc / "requests" / (request_id + ".json")).write_text(
            json.dumps({"kind": kind})
        )
    queue.run()
    assert (
        json.loads((queue.rpc / "responses" / (ids[0] + ".json")).read_text())["status"]
        == "complete"
    )
    assert not (queue.rpc / "responses" / (ids[2] + ".json")).exists()
    assert ids[2] not in queue.seen


def test_operator_pause_drains_records_and_defers_new_requests(tmp_path):
    import threading

    state = tmp_path / "state"
    state.mkdir()
    started, release, second_complete = (threading.Event() for _ in range(3))
    ids = [f"{index:032x}" for index in (1, 2)]

    def prepare(request, request_id):
        def call():
            if request_id == ids[0]:
                started.set()
                if not release.wait(3):
                    raise RuntimeError("test operation was not released")
            else:
                second_complete.set()
            return {"id": request_id}

        return call

    queue = LocalRequestQueue(
        tmp_path / "rpc",
        state,
        deadline=time.time() + 10,
        prepare=prepare,
        completed=lambda result: None,
        failure_feedback=lambda error, stage: {"message": str(error)},
        poll_seconds=0.001,
    )
    (queue.rpc / "requests" / (ids[0] + ".json")).write_text("{}")
    thread = threading.Thread(target=queue.run)
    thread.start()
    try:
        assert started.wait(3)
        # A timeout cannot silently capture an operation still in progress.
        with pytest.raises(TimeoutError, match="did not drain"):
            with queue.pause(timeout=0.02):
                pytest.fail("active provider operation was not drained")
        release.set()
        with queue.pause(timeout=3):
            result = json.loads((state / (ids[0] + ".result.json")).read_text())
            assert result == {"id": ids[0]}
            (queue.rpc / "requests" / (ids[1] + ".json")).write_text("{}")
            assert not second_complete.is_set()
            assert not (state / (ids[1] + ".request.json")).exists()
        assert second_complete.wait(3)
    finally:
        release.set()
        queue.stop()
        thread.join(timeout=3)
    assert not thread.is_alive()
    with pytest.raises(RuntimeError, match="stopped"):
        with queue.pause(timeout=0.1):
            pytest.fail("stopped queue cannot be checkpointed")
