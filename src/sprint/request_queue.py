"""Operator-side file transport for task handlers on an external CPU host."""

import concurrent.futures
from contextlib import ExitStack
import json
import time
import threading
import traceback
import uuid
from pathlib import Path

from .file_io import read_regular_json


class LocalRequestQueue:
    """Serialize calls per resource group and publish task-redacted feedback.

    ``prepare(request, request_id)`` validates on the host thread and returns a
    zero-argument callable. By default all calls share one worker thread. Optional
    ``groups`` maps request kinds to independent, serial resource groups. Inline
    kinds run on the host thread, even while worker calls are pending.
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
        groups=None,
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
        self.groups = dict(groups or {})
        if any(
            not isinstance(value, str) or not value
            for pair in self.groups.items()
            for value in pair
        ):
            raise ValueError("request groups must map nonempty kinds to group names")
        self.poll_seconds = poll_seconds
        self.idle_seconds = idle_seconds
        self.seen = set()
        self.stopped = threading.Event()
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

    def stop(self):
        """Stop admission and drain prepared calls; safe from a signal handler."""
        self.stopped.set()

    def accepting(self):
        return not self.stopped.is_set() and time.time() < self.deadline

    def run(self):
        pending = {}
        pools = {}
        next_idle = time.monotonic()
        with ExitStack() as cleanup:
            while self.accepting() or pending:
                for group, (active_id, future) in list(pending.items()):
                    if not future.done():
                        continue
                    try:
                        self.finish(active_id, future.result())
                    except Exception as error:
                        self.fail(active_id, error, stage="service", status="failed")
                    del pending[group]
                if (
                    self.accepting()
                    and not pending
                    and self.idle
                    and time.monotonic() >= next_idle
                ):
                    self.idle()
                    next_idle = time.monotonic() + self.idle_seconds
                if self.accepting():
                    for path in sorted((self.rpc / "requests").glob("*.json")):
                        if not self.accepting():
                            break
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
                            group = self.groups.get(request.get("kind"), "default")
                            if group in pending and not inline:
                                self.publish(
                                    request_id,
                                    {
                                        "status": "rejected",
                                        "failure_stage": "busy",
                                        "active_request_id": pending[group][0],
                                        "resource_group": group,
                                        "message": "Request was not started because another job is active in this resource group.",
                                    },
                                )
                                continue
                            call = self.prepare(request, request_id)
                            if inline:
                                self.finish(request_id, call(), validate=False)
                            else:
                                if group not in pools:
                                    pools[group] = cleanup.enter_context(
                                        concurrent.futures.ThreadPoolExecutor(
                                            max_workers=1
                                        )
                                    )
                                pending[group] = (request_id, pools[group].submit(call))
                                self.publish(request_id, {"status": "running"})
                        except Exception as error:
                            self.fail(
                                request_id,
                                error,
                                stage="request_validation",
                                status="rejected",
                            )
                time.sleep(self.poll_seconds)
