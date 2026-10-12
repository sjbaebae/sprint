"""Host-mediated local queue; the learner never needs provider network access."""

import asyncio
import json
import re
import tempfile
from pathlib import Path
from datetime import UTC, datetime

from .workers import WorkerSession, close_all


class ResourceBroker:
    """Route bounded code requests to explicitly granted per-trial workers."""

    def __init__(
        self,
        environment,
        workers,
        *,
        directory: Path,
        backend,
        contexts=None,
        budget=None,
    ):
        if environment is None:
            raise ValueError("resource broker requires a live agent environment")
        self.environment = environment
        self.workers = workers
        self.directory = Path(directory)
        self.backend = backend
        self.contexts = contexts or {}
        self.sessions = {}
        self.task = None
        self.seen = set()
        self.completed = {}
        from .spec import Budget

        self.budget = budget or Budget()
        self.reservations = {}
        self.session_gate = asyncio.Lock()
        self.slots = asyncio.Semaphore(self.budget.worker_concurrency)
        self.gate = asyncio.Lock()

    async def start(self):
        self.directory.mkdir(parents=True, exist_ok=False)
        self.directory.chmod(0o700)
        self.task = asyncio.create_task(self.serve())

    async def serve(self):
        """Poll through the provider control plane, with no learner-facing socket."""
        while True:
            async with self.gate:
                requests = await self.read_queue()
                async with asyncio.TaskGroup() as group:
                    for request in requests:
                        group.create_task(self.bounded_dispatch(request))
            await asyncio.sleep(1)

    async def read_queue(self):
        """Retry observation only; valid requests remain until final feedback."""
        for attempt in range(3):
            try:
                async with asyncio.timeout(35):
                    result = await self.environment.exec(
                        "python -I -S /opt/sprint-resource.py _read",
                        user="root",
                        timeout_sec=30,
                    )
                if result.return_code != 0:
                    raise RuntimeError(
                        "resource queue read failed: " + str(result.stderr)
                    )
                requests = json.loads(result.stdout or "[]")
                if not isinstance(requests, list) or len(requests) > 32:
                    raise ValueError("invalid resource queue response")
                return requests
            except Exception as error:
                with (self.directory / "queue.jsonl").open("a") as stream:
                    stream.write(
                        json.dumps(
                            {
                                "timestamp": datetime.now(UTC).isoformat(),
                                "attempt": attempt + 1,
                                "error_type": type(error).__name__,
                                "message": str(error),
                            }
                        )
                        + "\n"
                    )
                if attempt == 2:
                    raise RuntimeError(
                        "resource queue read failed after 3 attempts"
                    ) from error
                await asyncio.sleep(attempt + 1)

    async def bounded_dispatch(self, request):
        """Drain a bounded concurrent batch before a checkpoint can acquire gate."""
        async with self.slots:
            await self.dispatch(request)

    def reserve(self, name):
        """Reserve the worker's full declared lifetime; never refund on uncertainty.

        This is a conservative operator rate budget, not a provider invoice cap.
        The reservation survives checkpoints and failed provisioning attempts.
        """
        if name in self.reservations:
            return
        worker = self.workers[name]
        rate = worker.hourly_cost
        if self.budget.worker_spend is not None and rate is None:
            raise ValueError("spending limit requires hourly_cost on every worker")
        amount = (rate or 0) * worker.lifetime_seconds / 3600
        if (
            self.budget.worker_spend is not None
            and sum(self.reservations.values()) + amount > self.budget.worker_spend
        ):
            raise ValueError("worker lifetime reservation exceeds spending limit")
        self.reservations[name] = amount
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / "spending.json").write_text(
            json.dumps(
                {
                    "basis": "declared-rate-full-lifetime",
                    "reservations": self.reservations,
                    "limit": self.budget.worker_spend,
                }
            )
        )

    async def session(self, name):
        """Serialize creation and reservation, including requests for the same worker."""
        async with self.session_gate:
            if name not in self.sessions:
                self.reserve(name)
                session = WorkerSession(
                    self.workers[name],
                    directory=self.directory / name,
                    backend=self.backend,
                    context=self.contexts.get(name),
                )
                await session.__aenter__()
                self.sessions[name] = session
            return self.sessions[name]

    async def dispatch(self, request):
        request_id = request["request_id"]
        if not isinstance(request_id, str) or not re.fullmatch(
            r"[a-f0-9]{32}", request_id
        ):
            raise ValueError("invalid queue request ID")
        if request_id in self.seen:
            return
        if len(self.seen) >= self.budget.resource_requests:
            await self.reject_limit(request_id)
            return
        self.seen.add(request_id)
        response = {"request_id": request_id}
        try:
            payload = request["payload"]
            if isinstance(payload, dict) and set(payload) == {"collect", "path"}:
                original = payload["collect"]
                if not isinstance(original, str) or original not in self.completed:
                    raise ValueError(
                        "collection requires a completed request from this trial"
                    )
                name = self.completed[original]
                destination = self.directory / (request_id + ".artifact")
                response.update(
                    await self.sessions[name].collect(payload["path"], destination)
                )
                target = "/resources/control/artifact-" + request_id
                await self.environment.upload_file(destination, target)
                permissions = await self.environment.exec(
                    "chmod 444 " + target, user="root", timeout_sec=30
                )
                if permissions.return_code != 0:
                    raise RuntimeError(
                        "failed to expose collected checkpoint read-only"
                    )
                response.update(status="completed", artifact=target)
            else:
                if not isinstance(payload, dict) or set(payload) not in (
                    {"resource", "code"},
                    {"resource", "command"},
                ):
                    raise ValueError(
                        "request must contain resource and either code or command"
                    )
                name = payload["resource"]
                if not isinstance(name, str) or name not in self.workers:
                    raise ValueError("resource is not granted to this trial")
                session = await self.session(name)
                result = (
                    await session.shell(payload["command"])
                    if "command" in payload
                    else await session.run(payload["code"])
                )
                response.update(result)
                response["status"] = (
                    "completed" if response["return_code"] == 0 else "failed"
                )
                if response["status"] == "completed":
                    self.completed[request_id] = name
        except Exception as error:
            response.update(
                status="error", error_type=type(error).__name__, message=str(error)
            )
        local = self.directory / (request_id + ".json")
        local.write_text(json.dumps(response))
        await self.publish(local, request_id)

    async def reject_limit(self, request_id):
        """Return explicit rejection without growing admitted lineage or host records.

        Rejection feedback remains in the learner sandbox. Retain one exhaustion
        event on the operator host; never allocate another worker or reservation.
        """
        response = {
            "request_id": request_id,
            "status": "error",
            "error_type": "ResourceRequestLimitError",
            "message": "trial resource request limit exceeded",
        }
        event = self.directory / "request-limit.json"
        if not event.exists():
            event.write_text(
                json.dumps(
                    {
                        "timestamp": datetime.now(UTC).isoformat(),
                        "event": "resource-request-limit-reached",
                        "first_rejected_request": request_id,
                        "limit": self.budget.resource_requests,
                    }
                )
            )
        with tempfile.TemporaryDirectory(prefix="sprint-rejection-") as temporary:
            local = Path(temporary) / "feedback.json"
            local.write_text(json.dumps(response))
            await self.publish(local, request_id)

    async def publish(self, local, request_id):
        """Retry atomic delivery of retained feedback; never repeat job execution."""
        target = "/resources/control/results/" + request_id + ".json"
        staging = "/resources/control/results/." + request_id + ".tmp"
        for attempt in range(3):
            try:
                async with asyncio.timeout(35):
                    await self.environment.upload_file(local, staging)
                    result = await self.environment.exec(
                        f"chown root:root {staging} && chmod 444 {staging} && mv -f -- {staging} {target}",
                        user="root",
                        timeout_sec=30,
                    )
                    if result.return_code != 0:
                        raise RuntimeError(
                            "feedback publication failed: " + str(result.stderr)
                        )
                return
            except Exception as error:
                with (self.directory / "delivery.jsonl").open("a") as stream:
                    stream.write(
                        json.dumps(
                            {
                                "timestamp": datetime.now(UTC).isoformat(),
                                "request_id": request_id,
                                "attempt": attempt + 1,
                                "error_type": type(error).__name__,
                                "message": str(error),
                            }
                        )
                        + "\n"
                    )
                if attempt == 2:
                    raise RuntimeError(
                        "resource feedback delivery failed after 3 attempts"
                    ) from error
                await asyncio.sleep(attempt + 1)

    async def close(self):
        """Cancel active code and destroy all workers before verifier execution."""
        failure = None
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            except Exception as error:
                failure = error
            self.task = None
        for session in self.sessions.values():
            session.closed = True
        try:
            await close_all(self.sessions.values())
        except Exception as cleanup_error:
            if failure is not None:
                raise ExceptionGroup(
                    "resource service and cleanup failed", [failure, cleanup_error]
                ) from None
            raise
        if failure is not None:
            raise failure


async def finalize_brokers(brokers, directory: Path):
    """Retry outstanding teardown and retain failures without losing trial results.

    This is campaign-final cleanup. Trial hooks already report their failures on
    the affected trial; remaining provider handles get one more bounded attempt.
    Cancellation is never swallowed. Provider TTLs remain the host-loss fallback.
    """
    errors = []

    async def attempt(name, broker):
        try:
            await broker.close()
        except Exception as error:
            errors.append(
                {
                    "timestamp": datetime.now(UTC).isoformat(),
                    "trial": name,
                    "error_type": type(error).__name__,
                    "message": str(error),
                }
            )

    async with asyncio.TaskGroup() as group:
        for name, broker in brokers.items():
            group.create_task(attempt(name, broker))
    if errors:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "cleanup-errors.json").write_text(json.dumps(errors, indent=2))
