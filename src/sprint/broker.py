"""Host-mediated local queue; the learner never needs provider network access."""

import asyncio
import base64
import json
import re
import shlex
import tempfile
import traceback
from pathlib import Path
from datetime import UTC, datetime

from .workers import WorkerSession, close_all
from .learner_ssh import connection_files, create_identity


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
        services=None,
    ):
        if environment is None:
            raise ValueError("resource broker requires a live agent environment")
        self.environment = environment
        self.workers = workers
        self.directory = Path(directory)
        self.backend = backend
        self.contexts = contexts or {}
        self.sessions = {}
        self.services = dict(services or {})
        self.service_handlers = {}
        self.service_call_gates = {}
        self.finalized_services = set()
        self.closing = False
        self.cleanup_pending = False
        self.service_gates = {name: asyncio.Lock() for name in self.services}
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
                if self.closing:
                    raise RuntimeError("cannot provision workers during shutdown")
                self.reserve(name)
                session = WorkerSession(
                    self.workers[name],
                    directory=self.directory / name,
                    backend=self.backend,
                    context=self.contexts.get(name),
                    learner_ssh_public_key=self.ssh_key(name),
                )
                await session.__aenter__()
                self.sessions[name] = session
            return self.sessions[name]

    def ssh_key(self, name):
        """Keep worker-scoped identities in private, checkpointed broker state."""
        worker = self.workers[name]
        if (
            worker.shell
            and worker.sandbox.internet != "none"
            and (worker.backend or self.backend) == "modal"
        ):
            return create_identity(self.directory / ("ssh-" + name))
        return None

    async def connect(self, name, session):
        """Publish SSH access to a normal attached worker, including after restore."""
        worker = self.workers[name]
        if (
            not worker.shell
            or worker.sandbox.internet == "none"
            or (worker.backend or self.backend) != "modal"
        ):
            raise ValueError("SSH requires a network-enabled Modal shell worker")
        await session.start()
        target = "/resources/control/connections/" + name
        hosts, config = connection_files(
            session.learner_ssh_endpoint, target + "/identity", target + "/known_hosts"
        )
        result = await self.environment.exec(
            "mkdir -p -- " + shlex.quote(target), user="root", timeout_sec=30
        )
        if result.return_code:
            raise RuntimeError("failed to prepare worker connection directory")
        with tempfile.TemporaryDirectory(prefix="sprint-ssh-") as temporary:
            local = Path(temporary)
            (local / "known_hosts").write_text(hosts)
            (local / "config").write_text(config)
            for source, filename in (
                (self.directory / ("ssh-" + name), "identity"),
                (local / "known_hosts", "known_hosts"),
                (local / "config", "config"),
            ):
                await self.environment.upload_file(source, target + "/" + filename)
        result = await self.environment.exec(
            "chown root:root "
            + shlex.quote(target)
            + " && chmod 755 "
            + shlex.quote(target)
            + " && chown agent:agent "
            + shlex.quote(target + "/identity")
            + " && chmod 400 "
            + shlex.quote(target + "/identity")
            + " && chmod 444 "
            + shlex.join([target + "/config", target + "/known_hosts"]),
            user="root",
            timeout_sec=30,
        )
        if result.return_code:
            raise RuntimeError("failed to protect worker connection files")
        return {
            "return_code": 0,
            "ssh_config": target + "/config",
            "command": "ssh -F " + shlex.quote(target + "/config") + " gpu",
        }

    async def service(self, name, value, request_id):
        """Serialize trusted handlers; retain diagnostics privately on failure."""
        from .services import ServiceContext, load_handler

        if not isinstance(name, str) or name not in self.services:
            raise ValueError("service is not granted to this trial")
        async with self.service_gates[name]:
            context = ServiceContext(self, name, request_id)
            (context.state / (request_id + ".request.json")).write_text(
                json.dumps(value, allow_nan=False)
            )
            try:
                if name not in self.service_handlers:
                    bundle, entrypoint = self.services[name]
                    self.service_handlers[name] = load_handler(bundle, entrypoint)
                result = await self.service_handlers[name](context, value)
                encoded = json.dumps(result, allow_nan=False)
                if len(encoded.encode()) > 2 * 1024 * 1024:
                    raise ValueError("service response exceeds 2 MiB")
                return result
            except Exception as error:
                (context.state / (request_id + ".error.txt")).write_text(
                    traceback.format_exc()
                )
                raise RuntimeError(
                    "Task service failed; operator diagnostics were retained."
                ) from error

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
            if isinstance(payload, dict) and set(payload) == {"service", "input"}:
                result = await self.service(
                    payload["service"], payload["input"], request_id
                )
                response.update(status="completed", result=result)
            elif isinstance(payload, dict) and set(payload) == {"collect", "path"}:
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
                    {"resource", "content", "path"},
                    {"resource", "connect"},
                ):
                    raise ValueError(
                        "request must contain resource and connect, code, command, or content/path"
                    )
                name = payload["resource"]
                if not isinstance(name, str) or name not in self.workers:
                    raise ValueError("resource is not granted to this trial")
                if "connect" in payload and (
                    payload["connect"] is not True
                    or not self.workers[name].shell
                    or self.workers[name].sandbox.internet == "none"
                    or (self.workers[name].backend or self.backend) != "modal"
                ):
                    raise ValueError(
                        "SSH requires a network-enabled Modal shell worker"
                    )
                session = await self.session(name)
                if "connect" in payload:
                    result = await self.connect(name, session)
                elif "content" in payload:
                    encoded = payload["content"]
                    if not isinstance(encoded, str) or len(encoded) > 1400000:
                        raise ValueError("upload exceeds 1 MiB")
                    result = await session.upload(
                        base64.b64decode(encoded, validate=True), payload["path"]
                    )
                elif "command" in payload:
                    result = await session.shell(payload["command"])
                else:
                    result = await session.run(payload["code"])
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
        from .services import ServiceContext, load_handler

        self.closing = True
        failures = []
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            except Exception as error:
                failures.append(error)
            self.task = None
        # Recovery restores private service state before any new request loads code.
        # A trial that ends immediately must still run its retained service audits.
        for name, (bundle, entrypoint) in self.services.items():
            if name in self.service_handlers:
                continue
            if not (self.directory / ("service-" + name)).is_dir():
                continue
            try:
                self.service_handlers[name] = load_handler(bundle, entrypoint)
            except Exception as error:
                failures.append(
                    RuntimeError(
                        f"service {name} could not load for finalization: {error}"
                    )
                )
        for name, handler in self.service_handlers.items():
            finalize = getattr(handler, "finalize", None)
            if finalize is None or name in self.finalized_services:
                continue
            # Cleanup retries must not repeat an irreversible task audit.
            self.finalized_services.add(name)
            try:
                context = ServiceContext(self, name)
                async with asyncio.timeout(60):
                    await finalize(context)
                context.record("finalization", {"status": "complete"})
            except asyncio.CancelledError as error:
                failures.append(error)
                break
            except Exception as error:
                try:
                    context = ServiceContext(self, name)
                    (context.state / "finalization.error.txt").write_text(
                        traceback.format_exc()
                    )
                    context.record("finalization", {"status": "failed"})
                except Exception as diagnostic_error:
                    failures.append(diagnostic_error)
                failures.append(
                    RuntimeError(f"service {name} finalization failed: {error}")
                )
        from .service_calls import cleanup_calls

        self.cleanup_pending = False
        for name in self.services:
            if not (self.directory / ("service-" + name)).is_dir():
                continue
            try:
                await cleanup_calls(ServiceContext(self, name))
            except Exception as error:
                self.cleanup_pending = True
                failures.append(error)
        for session in self.sessions.values():
            session.closed = True
        try:
            await close_all(self.sessions.values())
        except Exception as error:
            failures.append(error)
        if len(failures) == 1:
            raise failures[0]
        if failures:
            raise BaseExceptionGroup("resource service and cleanup failed", failures)


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
