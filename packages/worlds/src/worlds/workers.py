"""Operator-side worker lifecycle. Provider credentials never enter job payloads."""

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


class WorkerSession:
    """Own one lazily provisioned worker and execute bounded Python jobs serially.

    The workspace survives successful jobs for checkpoint reuse. Any execution
    exception destroys the worker: a timeout must not leave detached code running.
    Use as an async context manager so trial cancellation also tears it down.
    """

    def __init__(self, worker, *, directory: Path, backend="docker", factory=None):
        self.worker = worker
        self.directory = Path(directory)
        self.backend = backend
        self.factory = factory
        self.environment = None
        self.lock = asyncio.Lock()
        self.closed = False

    async def __aenter__(self):
        self.directory.mkdir(parents=True, exist_ok=False)
        self.directory.chmod(0o700)
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
        if self.environment is not None:
            return
        from harbor.environments.factory import EnvironmentFactory
        from harbor.models.task.config import (
            EnvironmentConfig,
            NetworkPolicy,
            NetworkMode,
        )
        from harbor.models.environment_type import EnvironmentType
        from harbor.models.trial.config import EnvironmentConfig as BackendConfig
        from harbor.models.trial.paths import TrialPaths
        from .backends.harbor import dockerfile

        build = self.directory / "environment"
        build.mkdir(exist_ok=True)
        (build / "Dockerfile").write_text(
            dockerfile(self.worker.sandbox, copy_workspace=False)
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
            config=BackendConfig(type=EnvironmentType(self.backend)),
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
            job_id = uuid4().hex
            source = self.directory / (job_id + ".py")
            source.write_text(code)
            self.record(
                "job-start",
                job_id=job_id,
                sha256=hashlib.sha256(code.encode()).hexdigest(),
            )
            try:
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
                        timeout_sec=self.worker.seconds,
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

    async def close(self):
        """Destroy the sandbox, including any learner-created detached processes."""
        if self.environment is not None:
            environment = self.environment
            await environment.stop(delete=True)
            self.environment = None
            self.record("worker-stopped")
