"""Operator-side file transport for task handlers on an external CPU host."""

import concurrent.futures
import json
import time
import traceback
import uuid
from pathlib import Path

from .file_io import read_regular_json


class LocalRequestQueue:
    """Serialize prepared calls and publish bounded, task-redacted feedback.

    ``prepare(request, request_id)`` validates on the host thread and returns a
    zero-argument callable. Normal calls run on one worker thread; explicitly
    inline kinds run on the host thread, even while a worker call is pending.
    Provider deadlines and cancellation belong to the supplied handlers. The
    queue stops admission at its wall-clock deadline and drains admitted work.
    """

    def __init__(
        self,
        rpc,
        state,
        *,
        deadline,
        prepare,
        completed,
        failure_feedback,
        idle=None,
        inline_kinds=(),
        poll_seconds=0.5,
        idle_seconds=60,
    ):
        self.rpc = Path(rpc)
        self.state = Path(state)
        self.deadline = deadline
        self.prepare = prepare
        self.completed = completed
        self.failure_feedback = failure_feedback
        self.idle = idle
        self.inline_kinds = frozenset(inline_kinds)
        self.poll_seconds = poll_seconds
        self.idle_seconds = idle_seconds
        self.seen = set()
        for name in ("requests", "responses"):
            (self.rpc / name).mkdir(parents=True, exist_ok=True)

    def publish(self, request_id, value):
        temporary = self.rpc / "responses" / (request_id + ".tmp")
        temporary.write_text(json.dumps(value, allow_nan=False))
        temporary.chmod(0o444)
        temporary.replace(temporary.with_suffix(".json"))

    def finish(self, request_id, result, *, validate=True):
        if validate:
            self.completed(result)
        (self.state / (request_id + ".result.json")).write_text(
            json.dumps(result, allow_nan=False)
        )
        self.publish(request_id, {"status": "complete", "result": result})

    def fail(self, request_id, error, *, stage, status):
        (self.state / (request_id + ".error.txt")).write_text(traceback.format_exc())
        self.publish(
            request_id,
            {
                "status": status,
                **self.failure_feedback(error, stage),
            },
        )

    def run(self):
        pending = None
        active_id = None
        next_idle = time.monotonic()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            while time.time() < self.deadline or pending is not None:
                if pending is not None and pending.done():
                    try:
                        self.finish(active_id, pending.result())
                    except Exception as error:
                        self.fail(active_id, error, stage="service", status="failed")
                    pending = None
                if pending is None and self.idle and time.monotonic() >= next_idle:
                    self.idle()
                    next_idle = time.monotonic() + self.idle_seconds
                if time.time() < self.deadline:
                    for path in sorted((self.rpc / "requests").glob("*.json")):
                        request_id = path.stem
                        if request_id in self.seen:
                            continue
                        self.seen.add(request_id)
                        try:
                            uuid.UUID(request_id)
                            request = read_regular_json(path, 1024 * 1024)
                            if not isinstance(request, dict):
                                raise ValueError("invalid request")
                            (self.state / (request_id + ".request.json")).write_text(
                                json.dumps(request, allow_nan=False)
                            )
                            inline = request.get("kind") in self.inline_kinds
                            if pending is not None and not inline:
                                self.publish(
                                    request_id,
                                    {
                                        "status": "rejected",
                                        "failure_stage": "busy",
                                        "active_request_id": active_id,
                                        "message": "Request was not started because another job is active.",
                                    },
                                )
                                continue
                            call = self.prepare(request, request_id)
                            if inline:
                                self.finish(request_id, call(), validate=False)
                            else:
                                pending = pool.submit(call)
                                active_id = request_id
                                self.publish(request_id, {"status": "running"})
                        except Exception as error:
                            self.fail(
                                request_id,
                                error,
                                stage="request_validation",
                                status="rejected",
                            )
                time.sleep(self.poll_seconds)
