"""Operator-side worker lifecycle. Provider credentials never enter job payloads."""

import asyncio
import hashlib
import json
import math
import os
import stat
import shlex
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from uuid import UUID, uuid4

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


CLEANUP_SECONDS = 60
MODAL_WORKER_APP = "sprint-workers"


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
    """Own one lazy worker and serialize its Python jobs or granted shell commands.

    Files survive successful calls; shell calls may also retain processes. Shell command
    deadlines return a process exit status; provider errors and lifetime expiry
    destroy the worker. Python jobs retain their per-job process cleanup.
    Use as an async context manager so trial cancellation also tears it down.
    """

    def __init__(
        self,
        worker,
        *,
        directory: Path,
        backend="docker",
        factory=None,
        context=None,
        artifact_volume: str | None = None,
        learner_ssh_public_key: str | None = None,
        session_id: str | None = None,
    ):
        self.worker = worker
        self.session_id = (
            UUID(session_id).hex if session_id is not None else uuid4().hex
        )
        self.directory = Path(directory)
        self.backend = worker.backend or backend
        self.artifact_volume = artifact_volume
        self.learner_ssh_public_key = learner_ssh_public_key
        if learner_ssh_public_key and (self.backend != "modal" or not worker.shell):
            raise ValueError("learner SSH requires a Modal shell worker")
        if artifact_volume and self.backend != "modal":
            raise ValueError("artifact_volume requires the Modal backend")
        if worker.read_only_volumes and self.backend != "modal":
            raise ValueError("read_only_volumes currently requires the Modal backend")
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

    @property
    def learner_ssh_endpoint(self):
        """Return learner-only connection details after start; never a private key.

        The caller owns the matching private key and must pin the returned host
        key in its SSH known_hosts file. The endpoint expires with this worker.
        """
        if self.closed or self.environment is None:
            raise RuntimeError("worker is not running")
        endpoint = getattr(self.environment, "learner_ssh_endpoint", None)
        if endpoint is None:
            raise RuntimeError("learner SSH is not configured")
        return dict(endpoint)

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
        """Provision once within the resource lifetime, including provider setup."""
        if self.deadline is None:
            raise RuntimeError("worker session has not been entered")
        try:
            async with asyncio.timeout_at(self.deadline):
                await self._start()
        except BaseException:
            await self.close()
            raise

    async def _start(self):
        """Apply the network policy and protect storage before exposing a worker."""
        if self.closed:
            raise RuntimeError("worker session is closed")
        if self.deadline is None or self.deadline <= time.monotonic():
            raise TimeoutError("worker lifetime exceeded")
        if self.environment is not None:
            return
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
                        "app_name": MODAL_WORKER_APP,
                        "read_only_volumes": self.worker.read_only_volumes,
                        "learner_ssh_public_key": self.learner_ssh_public_key,
                        "volumes": (
                            {"/opt/sprint-artifacts": self.artifact_volume}
                            if self.artifact_volume
                            else {}
                        ),
                        "sandbox_timeout_secs": max(
                            1, math.ceil(self.deadline - time.monotonic())
                        ),
                    }
                    if self.backend == "modal"
                    else {}
                ),
            ),
            environment_dir=build,
            environment_name="worker-" + uuid4().hex[:12],
            session_id=self.session_id,
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
        if self.artifact_volume:
            result = await self.environment.exec(
                "chown root:root /opt/sprint-artifacts && chmod 700 /opt/sprint-artifacts",
                user="root",
                timeout_sec=30,
            )
            if result.return_code:
                await self.close()
                raise RuntimeError("could not protect artifact volume")
        self.record("worker-started")

    async def collect_to_volume(self, relative: str):
        """Freeze a checkpoint into a private Modal v2 volume without host relay.

        The operator creates and owns the volume. Its root-only mount is never
        exposed to agent commands. The caller owns retention and deletion.
        """
        if not self.artifact_volume:
            raise ValueError("artifact_volume is not configured")
        if not isinstance(relative, str):
            raise ValueError("checkpoint path must be text")
        async with self.lock:
            if self.deadline is None or self.deadline <= time.monotonic():
                await self.close()
                raise TimeoutError("worker lifetime exceeded")
            if self.closed or self.environment is None:
                raise RuntimeError("worker is not available for collection")
            try:
                async with asyncio.timeout_at(self.deadline):
                    result = await self.environment.exec(
                        "python -I -S /opt/sprint-export.py "
                        + shlex.quote(relative)
                        + " "
                        + str(self.worker.max_artifact_bytes)
                        + " --volume",
                        user="root",
                        timeout_sec=120,
                    )
                    if result.return_code:
                        raise ValueError(
                            "checkpoint collection failed: " + str(result.stderr)
                        )
                    metadata = json.loads(result.stdout or "{}")
                    path = PurePosixPath(metadata["path"])
                    if path.parent != PurePosixPath(
                        "/opt/sprint-artifacts"
                    ) or not path.name.startswith("sprint-export-"):
                        raise ValueError("invalid worker export path")
                    committed = await self.environment.exec(
                        "sync /opt/sprint-artifacts",
                        user="root",
                        timeout_sec=120,
                    )
                    if committed.return_code:
                        raise RuntimeError("artifact volume commit failed")
                    receipt = {
                        "volume": self.artifact_volume,
                        "path": path.name,
                        "sha256": metadata["sha256"],
                        "bytes": metadata["bytes"],
                    }
                    self.record(
                        "checkpoint-collected-to-volume", source=relative, **receipt
                    )
                    return receipt
            except ValueError:
                raise
            except BaseException:
                await self.close()
                raise

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
                    seconds = max(
                        1,
                        math.ceil(
                            min(
                                self.worker.seconds,
                                max(0, self.deadline - time.monotonic()),
                            )
                        ),
                    )
                    result = await asyncio.wait_for(
                        environment.exec(
                            f"python -I -S /opt/sprint-run.py /app/{job_id}.py {seconds}",
                            cwd="/app",
                            user="root",
                            timeout_sec=seconds + 20,
                        ),
                        timeout=seconds + 30,
                    )
                if result.return_code != 0:
                    raise RuntimeError("worker launcher failed: " + str(result.stderr))
                payload = json.loads(result.stdout or "{}")
                if (
                    type(payload.get("return_code")) is not int
                    or not isinstance(payload.get("stdout"), str)
                    or not isinstance(payload.get("stderr"), str)
                ):
                    raise ValueError("invalid worker launcher feedback")
                payload["job_id"] = job_id
                self.record("job-end", **payload)
                if payload.get("timed_out"):
                    await self.close()
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

    async def shell(self, command: str, *, seconds: int | None = None):
        """Run a bounded shell command in the persistent learner workspace.

        Unlike Python jobs, successful shell calls leave background processes
        running. A command timeout returns exit code 124 and preserves the workspace.
        Provider failures, trial expiry and cancellation destroy the worker.
        Provider credentials remain exclusively on the orchestration host.
        """
        if not self.worker.shell:
            raise PermissionError("shell access is not granted to this worker")
        if (
            not isinstance(command, str)
            or not command.strip()
            or len(command.encode()) > 65536
        ):
            raise ValueError("shell command must be nonempty text of at most 64 KiB")
        limit = self.worker.seconds if seconds is None else seconds
        if type(limit) is not int or not 1 <= limit <= self.worker.seconds:
            raise ValueError("shell timeout exceeds worker command budget")
        async with self.lock:
            if self.closed or self.deadline is None:
                raise RuntimeError("worker session is not active")
            job_id = uuid4().hex
            self.record("shell-start", job_id=job_id, command=command)
            try:
                async with asyncio.timeout_at(self.deadline):
                    await self.start()
                    if self.environment is None:
                        raise RuntimeError("worker failed to initialize")
                    seconds = max(
                        1,
                        math.ceil(min(limit, self.deadline - time.monotonic())),
                    )
                    result = await asyncio.wait_for(
                        self.environment.exec(
                            f"timeout --signal=TERM --kill-after=5 {seconds}s "
                            "/bin/bash -o pipefail -c " + shlex.quote(command),
                            cwd="/app",
                            user="agent",
                            timeout_sec=seconds + 10,
                        ),
                        timeout=seconds + 20,
                    )
                payload = {
                    "job_id": job_id,
                    "return_code": result.return_code,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                }
                self.record("shell-end", **payload)
                return payload
            except BaseException as error:
                self.record(
                    "shell-error",
                    job_id=job_id,
                    error_type=type(error).__name__,
                    message=str(error),
                )
                await self.close()
                raise

    async def upload(self, content: bytes, relative: str):
        """Copy a small learner payload; larger files use operator upload_file()."""
        if not isinstance(content, bytes) or len(content) > 1024 * 1024:
            raise ValueError("upload must contain at most 1 MiB")
        source = self.directory / (uuid4().hex + ".inline")
        source.write_bytes(content)
        try:
            return await self.upload_file(source, relative, max_bytes=1024 * 1024)
        finally:
            source.unlink(missing_ok=True)

    async def upload_file(self, source: Path, relative: str, *, max_bytes=1024**3):
        """Snapshot an operator-supplied regular file and upload it beneath /app.

        This host API can transport screened datasets without putting their contents
        in the learner RPC queue. No arbitrary host path is exposed to the learner.
        """
        path = PurePosixPath(relative) if isinstance(relative, str) else None
        if path is None or path.is_absolute() or ".." in path.parts or not path.name:
            raise ValueError("upload destination must be a relative file beneath /app")
        if type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("upload limit must be a positive integer")
        async with self.lock:
            if self.closed or self.deadline is None:
                raise RuntimeError("worker session is not active")
            staging = "/tmp/sprint-upload-" + uuid4().hex
            snapshot = self.directory / (uuid4().hex + ".upload")
            try:
                descriptor = os.open(
                    source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                )
                with os.fdopen(descriptor, "rb") as stream:
                    before = os.fstat(stream.fileno())
                    if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
                        raise ValueError("upload must be a bounded regular file")
                    digest = hashlib.sha256()
                    size = 0
                    with snapshot.open("xb") as target:
                        while block := stream.read(1024 * 1024):
                            size += len(block)
                            if size > max_bytes:
                                raise ValueError("upload exceeds byte limit")
                            digest.update(block)
                            target.write(block)
                    after = os.fstat(stream.fileno())
                    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                        after.st_size,
                        after.st_mtime_ns,
                        after.st_ctime_ns,
                    ):
                        raise ValueError("upload source changed during snapshot")
                async with asyncio.timeout_at(self.deadline):
                    await self.start()
                    environment = self.environment
                    if environment is None:
                        raise RuntimeError("worker failed to initialize")
                    await environment.upload_file(snapshot, staging)
                    result = await environment.exec(
                        "chmod 444 " + staging, user="root", timeout_sec=30
                    )
                    if result.return_code != 0:
                        raise RuntimeError("cannot stage worker upload")
                    script = (
                        "from pathlib import Path; import shutil; "
                        "root=Path('/app'); target=(root / "
                        + repr(relative)
                        + ").resolve(); "
                        "target.relative_to(root); target.parent.mkdir(parents=True, exist_ok=True); "
                        "shutil.copyfile(" + repr(staging) + ", target)"
                    )
                    result = await environment.exec(
                        "python -I -S -c " + shlex.quote(script),
                        user="agent",
                        cwd="/app",
                        timeout_sec=30,
                    )
                    if result.return_code != 0:
                        raise ValueError("worker upload failed: " + str(result.stderr))
                    digest = digest.hexdigest()
                    self.record(
                        "file-uploaded",
                        path=str(path),
                        bytes=size,
                        sha256=digest,
                    )
                    return {
                        "return_code": 0,
                        "path": "/app/" + str(path),
                        "bytes": size,
                        "sha256": digest,
                    }
            except ValueError:
                raise
            except BaseException:
                await self.close()
                raise
            finally:
                snapshot.unlink(missing_ok=True)
                if self.environment is not None:
                    await self.environment.exec(
                        "rm -f " + staging, user="root", timeout_sec=30
                    )

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
            "python -I -S /opt/sprint-export.py "
            + shlex.quote(relative)
            + " "
            + str(self.worker.max_artifact_bytes),
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
            size = Path(destination).stat().st_size
            if size != metadata["bytes"] or size > self.worker.max_artifact_bytes:
                raise ValueError("collected checkpoint size mismatch or limit exceeded")
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
