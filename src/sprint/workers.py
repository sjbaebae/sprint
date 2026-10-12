"""Operator-side worker lifecycle. Provider credentials never enter job payloads."""

import asyncio
import hashlib
import json
import math
import shlex
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


CLEANUP_SECONDS = 60


async def close_all(resources):
    """Attempt every teardown even if another provider reports a cleanup failure."""
    errors = []

    async def attempt(resource):
        try:
            await resource.close()
        except Exception as error:
            errors.append(error)

    async with asyncio.TaskGroup() as group:
        for resource in resources:
            group.create_task(attempt(resource))
    if errors:
        raise ExceptionGroup("resource cleanup failed", errors)


class WorkerSession:
    """Own one lazily provisioned worker and execute bounded Python jobs serially.

    The workspace survives successful jobs for checkpoint reuse. Any execution
    exception destroys the worker: a timeout must not leave detached code running.
    Use as an async context manager so trial cancellation also tears it down.
    """

    def __init__(
        self, worker, *, directory: Path, backend="docker", factory=None, context=None
    ):
        self.worker = worker
        self.directory = Path(directory)
        self.backend = worker.backend or backend
        self.factory = factory
        self.context = Path(context) if context is not None else None
        self.environment = None
        self.lock = asyncio.Lock()
        self.closed = False
        self.deadline = None

    async def __aenter__(self):
        self.directory.mkdir(parents=True, exist_ok=False)
        self.directory.chmod(0o700)
        self.deadline = time.monotonic() + self.worker.lifetime_seconds
        return self

    async def __aexit__(self, *_):
        self.closed = True
        await self.close()

    def record(self, event, **fields):
        """Append trusted job provenance outside the learner filesystem."""
        with (self.directory / "jobs.jsonl").open("a") as stream:
            stream.write(
                json.dumps(
                    {
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "event": event,
                        **fields,
                    }
                )
                + "\n"
            )

    async def start(self):
        """Provision once; apply the network policy at container creation."""
        if self.closed:
            raise RuntimeError("worker session is closed")
        if self.deadline is None or self.deadline <= time.monotonic():
            raise TimeoutError("worker lifetime exceeded")
        if self.environment is not None:
            return
        from sprint.environments.factory import EnvironmentFactory
        from sprint.models.task.config import (
            EnvironmentConfig,
            NetworkPolicy,
            NetworkMode,
        )
        from sprint.models.environment_type import EnvironmentType
        from sprint.models.trial.config import EnvironmentConfig as BackendConfig
        from sprint.models.trial.paths import TrialPaths
        from .backends.runtime import build_worker

        build = self.context or build_worker(
            self.worker, self.directory / "environment"
        )
        paths = TrialPaths(trial_dir=self.directory / "runtime")
        paths.mkdir()
        request = self.worker.resources
        sandbox = self.worker.sandbox
        policy = NetworkPolicy(
            network_mode=NetworkMode(
                {
                    "none": "no-network",
                    "full": "public",
                    "allowlist": "allowlist",
                }[sandbox.internet]
            ),
            allowed_hosts=list(sandbox.allowed_hosts),
        )
        factory = self.factory or EnvironmentFactory.create_environment_from_config
        self.environment = factory(
            config=BackendConfig(
                type=EnvironmentType(self.backend),
                kwargs=(
                    {
                        "sandbox_timeout_secs": max(
                            1, math.ceil(self.deadline - time.monotonic())
                        )
                    }
                    if self.backend == "modal"
                    else {}
                ),
            ),
            environment_dir=build,
            environment_name="worker-" + uuid4().hex[:12],
            session_id=uuid4().hex,
            trial_paths=paths,
            task_env_config=EnvironmentConfig(
                cpus=request.cpus,
                memory_mb=request.memory_mb,
                gpus=request.gpu_count if request.gpu else 0,
                gpu_types=[request.gpu] if request.gpu else [],
            ),
            network_policy=policy,
        )
        await self.environment.start(force_build=False)
        self.record("worker-started")

    async def run(self, code: str):
        """Execute source; return actual exit code/stdout/stderr and a host job ID.

        Jobs run as the unprivileged agent user in /app. Code may keep state under
        /app across jobs. No caller-controlled host paths or shell commands exist.
        """
        if not isinstance(code, str) or len(code.encode()) > 1024 * 1024:
            raise ValueError("job source must be text of at most 1 MiB")
        async with self.lock:
            if self.closed:
                raise RuntimeError("worker session is closed")
            if self.deadline is None:
                raise RuntimeError("worker session must be entered before use")
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                await self.close()
                raise TimeoutError("worker lifetime exceeded")
            job_id = uuid4().hex
            source = self.directory / (job_id + ".py")
            source.write_text(code)
            self.record(
                "job-start",
                job_id=job_id,
                sha256=hashlib.sha256(code.encode()).hexdigest(),
            )
            try:
                async with asyncio.timeout_at(self.deadline):
                    await self.start()
                    environment = self.environment
                    if environment is None:
                        raise RuntimeError("worker failed to initialize")
                    await environment.upload_file(source, "/app/" + job_id + ".py")
                    result = await asyncio.wait_for(
                        environment.exec(
                            "python /app/" + job_id + ".py",
                            cwd="/app",
                            user="agent",
                            timeout_sec=max(
                                1,
                                math.ceil(
                                    min(
                                        self.worker.seconds,
                                        max(0, self.deadline - time.monotonic()),
                                    )
                                ),
                            ),
                        ),
                        timeout=self.worker.seconds + 30,
                    )
                payload = {
                    "job_id": job_id,
                    "return_code": result.return_code,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                }
                self.record("job-end", **payload)
                return payload
            except BaseException as error:
                self.record(
                    "job-error",
                    job_id=job_id,
                    error_type=type(error).__name__,
                    message=str(error),
                )
                await self.close()
                raise

    async def collect(self, relative: str, destination: Path):
        """Return a stable regular-file snapshot from /app; retain its trusted hash."""
        if not isinstance(relative, str):
            raise ValueError("checkpoint path must be text")
        async with self.lock:
            if self.deadline is None or self.deadline <= time.monotonic():
                await self.close()
                raise TimeoutError("worker lifetime exceeded")
            try:
                async with asyncio.timeout_at(self.deadline):
                    return await self._collect(relative, destination)
            except ValueError:
                raise
            except BaseException:
                await self.close()
                raise

    async def _collect(self, relative, destination):
        """Collect under the caller's worker lock and lifetime deadline."""
        environment = self.environment
        if self.closed or environment is None:
            raise RuntimeError("worker is not available for collection")
        result = await environment.exec(
            "python -I -S /opt/sprint-export.py " + shlex.quote(relative),
            user="root",
            timeout_sec=120,
        )
        if result.return_code != 0:
            raise ValueError("checkpoint collection failed: " + str(result.stderr))
        metadata = json.loads(result.stdout or "{}")
        exported = metadata["path"]
        if (
            not isinstance(exported, str)
            or not exported.startswith("/tmp/sprint-export-")
            or "/" in exported.removeprefix("/tmp/")
        ):
            raise ValueError("invalid worker export path")
        try:
            await environment.download_file(exported, destination)
            with Path(destination).open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if digest != metadata["sha256"]:
                raise ValueError("collected checkpoint hash mismatch")
            self.record(
                "checkpoint-collected",
                source=relative,
                sha256=digest,
                bytes=metadata["bytes"],
            )
            return {"sha256": digest, "bytes": metadata["bytes"]}
        finally:
            await environment.exec(
                "rm -f " + shlex.quote(exported), user="root", timeout_sec=30
            )

    async def close(self):
        """Destroy the sandbox, including any learner-created detached processes."""
        self.closed = True
        if self.environment is not None:
            environment = self.environment
            try:
                await asyncio.wait_for(
                    environment.stop(delete=True), timeout=CLEANUP_SECONDS
                )
            except BaseException as error:
                self.record(
                    "worker-cleanup-error",
                    error_type=type(error).__name__,
                    message=str(error),
                )
                raise
            self.environment = None
            self.record("worker-stopped")
