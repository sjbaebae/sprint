"""Host-mediated local queue; the learner never needs provider network access."""

import asyncio
import json
import re
from pathlib import Path

from .workers import WorkerSession, close_all


class ResourceBroker:
    """Route bounded code requests to explicitly granted per-trial workers."""

    def __init__(
        self, environment, workers, *, directory: Path, backend, contexts=None
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

    async def start(self):
        self.directory.mkdir(parents=True, exist_ok=False)
        self.directory.chmod(0o700)
        self.task = asyncio.create_task(self.serve())

    async def serve(self):
        """Poll through the provider control plane, with no learner-facing socket."""
        while True:
            result = await self.environment.exec(
                "python -I -S /opt/sprint-resource.py _read",
                user="root",
                timeout_sec=30,
            )
            if result.return_code != 0:
                raise RuntimeError("resource queue read failed: " + str(result.stderr))
            for request in json.loads(result.stdout or "[]"):
                await self.dispatch(request)
            await asyncio.sleep(1)

    async def dispatch(self, request):
        request_id = request["request_id"]
        if not isinstance(request_id, str) or not re.fullmatch(
            r"[a-f0-9]{32}", request_id
        ):
            raise ValueError("invalid queue request ID")
        if request_id in self.seen:
            return
        self.seen.add(request_id)
        response = {"request_id": request_id}
        try:
            if len(self.seen) > 200:
                raise ValueError("trial resource request limit exceeded")
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
                if not isinstance(payload, dict) or set(payload) != {
                    "resource",
                    "code",
                }:
                    raise ValueError("request must contain only resource and code")
                name = payload["resource"]
                if not isinstance(name, str) or name not in self.workers:
                    raise ValueError("resource is not granted to this trial")
                if name not in self.sessions:
                    session = WorkerSession(
                        self.workers[name],
                        directory=self.directory / name,
                        backend=self.backend,
                        context=self.contexts.get(name),
                    )
                    await session.__aenter__()
                    self.sessions[name] = session
                response.update(await self.sessions[name].run(payload["code"]))
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
        await self.environment.upload_file(
            local, "/resources/control/results/" + request_id + ".json"
        )

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
        await close_all(self.sessions.values())
        if failure is not None:
            raise failure
