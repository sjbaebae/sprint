"""Definitions describe intent; backend-specific configuration stays out of tasks."""

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


RESERVED_MOUNTS = frozenset(
    "app tmp etc opt resources logs home root var sbin lib64 run usr bin lib dev proc sys".split()
)

STATE_PATHS = (
    "/app",
    "/home/agent",
    "/logs/agent",
    "/resources/control",
    "/installed-agent",
)


def validate_state_paths(paths):
    """Declare mutable directories; pseudo-filesystems cannot be checkpointed."""
    if not isinstance(paths, (tuple, list)) or not paths:
        raise ValueError("state_paths must be a nonempty sequence")
    parsed = []
    for value in paths:
        path = Path(value)
        if (
            not path.is_absolute()
            or ".." in path.parts
            or str(path) != value
            or path == Path("/")
        ):
            raise ValueError(
                "state_paths must be canonical absolute directories below root"
            )
        if any(
            path == Path(p) or path.is_relative_to(p) for p in ("/proc", "/sys", "/dev")
        ):
            raise ValueError("pseudo-filesystems cannot be checkpointed")
        if any(path.is_relative_to(p) or p.is_relative_to(path) for p in parsed):
            raise ValueError("state_paths must not overlap")
        parsed.append(path)


@dataclass(frozen=True)
class Sandbox:
    """Reusable execution image, Python tooling and network policy."""

    image: str = "python:3.12-slim"
    packages: tuple[str, ...] = ()
    setup: tuple[str, ...] = ()
    internet: str = "none"
    allowed_hosts: tuple[str, ...] = ()

    def __post_init__(self):
        for name, values in (("packages", self.packages), ("setup", self.setup)):
            if not isinstance(values, (tuple, list)) or any(
                not isinstance(value, str) or not value.strip() for value in values
            ):
                raise ValueError(f"{name} must be a sequence of nonempty strings")
        if self.internet not in {"none", "allowlist", "full"}:
            raise ValueError("internet must be none, allowlist, or full")
        if bool(self.allowed_hosts) != (self.internet == "allowlist"):
            raise ValueError(
                "allowlist requires hosts; other modes cannot specify hosts"
            )
        if not self.image or any(c.isspace() for c in self.image):
            raise ValueError("image must be a single container image reference")


@dataclass(frozen=True)
class Resources:
    """Hardware for one sandbox role, independent of image and task.

    The orchestration host is the process calling run(). These requests apply
    only to provisioned workers; validation has its own request and no default GPU.
    """

    cpus: int = 2
    memory_mb: int = 4096
    gpu: str | None = None
    gpu_count: int = 1

    def __post_init__(self):
        if any(
            type(x) is not int or x < 1
            for x in (self.cpus, self.memory_mb, self.gpu_count)
        ):
            raise ValueError("resource counts must be positive integers")
        if self.gpu is not None and not re.fullmatch(r"[A-Za-z0-9_-]+", self.gpu):
            raise ValueError("gpu must be an accelerator name such as L4 or A100")


@dataclass(frozen=True)
class Files:
    """Public directory attached read-only under /resources/<name>.

    Contents are copied at compilation, never mounted from the operator host.
    Protected evaluation data must remain in Validation.bundle.
    """

    path: Path


@dataclass(frozen=True)
class Worker:
    """Persistent worker for Python jobs, uploads and opt-in shell access.

    read_only_volumes maps dedicated mount paths to existing Modal asset volumes.
    These external assets must remain immutable across checkpoints and restores.
    """

    sandbox: Sandbox = field(default_factory=Sandbox)
    resources: Resources = field(default_factory=Resources)
    seconds: int = 600
    lifetime_seconds: int = 3600
    shell: bool = False
    read_only_volumes: dict[str, str] = field(default_factory=dict)
    max_artifact_bytes: int = 1024**3
    backend: str | None = None

    hourly_cost: float | None = None

    state_paths: tuple[str, ...] = STATE_PATHS

    def __post_init__(self):
        if type(self.max_artifact_bytes) is not int or self.max_artifact_bytes < 1:
            raise ValueError("max_artifact_bytes must be a positive integer")
        for mount, name in self.read_only_volumes.items():
            path = Path(mount)
            if (
                not path.is_absolute()
                or ".." in path.parts
                or str(path) != mount
                or len(path.parts) != 2
                or path.name in RESERVED_MOUNTS
            ):
                raise ValueError(
                    "read-only volumes require dedicated top-level mount paths such as /base"
                )
            if not isinstance(name, str) or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_.-]*", name
            ):
                raise ValueError(
                    "read-only volume name must be a provider volume identifier"
                )
        if type(self.shell) is not bool:
            raise ValueError("worker shell permission must be a boolean")
        if self.hourly_cost is not None and (
            type(self.hourly_cost) not in (int, float)
            or not math.isfinite(self.hourly_cost)
            or self.hourly_cost < 0
        ):
            raise ValueError("hourly_cost must be finite and nonnegative")
        validate_state_paths(self.state_paths)
        if self.backend is not None:
            from .models.environment_type import EnvironmentType

            EnvironmentType(self.backend)
            if self.backend == "allocated":
                raise ValueError(
                    "allocated is learner-only; workers require a container backend"
                )
        if type(self.lifetime_seconds) is not int or self.lifetime_seconds < 1:
            raise ValueError("worker lifetime must be a positive integer")
        if type(self.seconds) is not int or self.seconds < 1:
            raise ValueError("worker timeout must be a positive integer")


@dataclass(frozen=True)
class Service:
    """Private operator handler exposed through bounded JSON requests.

    bundle is compiled outside learner images. The async entrypoint handle(context,
    request) must keep durable state in context.state and use context.worker() for
    provider work, so native checkpoint and cleanup own that work.
    """

    bundle: Path
    entrypoint: str = "service.py"

    def __post_init__(self):
        path = Path(self.entrypoint)
        if path.is_absolute() or ".." in path.parts or not self.entrypoint:
            raise ValueError("service entrypoint must stay inside its private bundle")


@dataclass(frozen=True)
class Env:
    """Shared workspace and sandbox; reusable across tasks and evaluations.

    Files are copied into the learner workspace. Never put protected data here.
    Named Files are read-only in the learner and workers; Worker attachments
    expose code submission and checkpoint collection through the host broker.
    """

    sandbox: Sandbox = field(default_factory=Sandbox)
    files: Path | None = None
    resources: Resources = field(default_factory=Resources)
    attachments: dict[str, Files | Worker | Service] = field(default_factory=dict)

    state_paths: tuple[str, ...] = STATE_PATHS

    def __post_init__(self):
        validate_state_paths(self.state_paths)
        for name, attachment in self.attachments.items():
            if name == "control" or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]*", name):
                raise ValueError("attachment names must be simple identifiers")
            if not isinstance(attachment, (Files, Worker, Service)):
                raise TypeError("attachments must be Files, Worker or Service")


@dataclass(frozen=True)
class Task:
    """The objective and which workspace paths are handed to the verifier."""

    objective: str
    name: str = "research-task"
    submission: str = "/app/submission"

    def __post_init__(self):
        if not self.objective.strip():
            raise ValueError("objective is required")
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]*", self.name):
            raise ValueError(
                "task name must use letters, numbers, underscores or hyphens"
            )
        path = Path(self.submission)
        if (
            not path.is_absolute()
            or ".." in path.parts
            or not path.is_relative_to("/app")
            or path == Path("/app")
        ):
            raise ValueError("submission must be a path below /app")


@dataclass(frozen=True)
class Validation:
    """Trusted evaluation bundle, isolated from the learner.

    entrypoint defines verify(context), optionally judge(context, rubric) and
    disqualify(context). See the small function contract in README.md. Private
    files live in bundle; they are copied only into the verifier image.
    """

    bundle: Path
    entrypoint: str = "evaluate.py"
    rubric: str | None = None
    weights: tuple[float, float] = (1.0, 0.0)
    sandbox: Sandbox = field(default_factory=Sandbox)
    seconds: int = 600
    reference: Path | None = None
    resources: Resources = field(default_factory=Resources)
    backend: str | None = None

    def __post_init__(self):
        if self.backend not in {None, "docker", "modal"}:
            raise ValueError("validation backend must be docker, modal, or None")
        for name in (self.entrypoint, self.rubric):
            if name is not None and (
                Path(name).is_absolute() or ".." in Path(name).parts
            ):
                raise ValueError("validation paths must remain inside the bundle")
        if (
            len(self.weights) != 2
            or any(
                type(x) not in (int, float) or not math.isfinite(x) or x < 0
                for x in self.weights
            )
            or not math.isclose(sum(self.weights), 1)
        ):
            raise ValueError(
                "verifier and judge weights must be nonnegative and sum to one"
            )
        if self.weights[1] and not self.rubric:
            raise ValueError("model-based grading requires a rubric")
        if type(self.seconds) is not int or self.seconds <= 0:
            raise ValueError("validation timeout must be a positive integer")


@dataclass(frozen=True)
class Agent:
    """The solver model and its harness, independent of the task's target model."""

    model: str
    harness: str = "codex"
    options: dict[str, Any] = field(default_factory=dict)
    auth: str = "auto"

    def __post_init__(self):
        if self.auth not in {"auto", "api-key"}:
            raise ValueError("agent auth must be auto or api-key")
        if self.auth == "api-key" and self.harness not in {
            "codex",
            "bounded-codex",
            "claude-code",
        }:
            raise ValueError("explicit API-key auth supports codex and claude-code")


@dataclass(frozen=True)
class Budget:
    """Trial wall time. Harness-specific action limits require a capable adapter."""

    seconds: int = 3600
    worker_concurrency: int = 1
    resource_requests: int = 200
    worker_spend: float | None = None

    def __post_init__(self):
        for value in (self.worker_concurrency, self.resource_requests):
            if type(value) is not int or value < 1:
                raise ValueError(
                    "concurrency and request limits must be positive integers"
                )
        if self.worker_spend is not None and (
            type(self.worker_spend) not in (int, float)
            or not math.isfinite(self.worker_spend)
            or self.worker_spend < 0
        ):
            raise ValueError("worker_spend must be finite and nonnegative")
        if type(self.seconds) is not int or self.seconds <= 0:
            raise ValueError("trial timeout must be a positive integer")


@dataclass(frozen=True)
class Experiment:
    """Combine independent definitions, then compile or execute on a backend."""

    task: Task
    validation: Validation
    agent: Agent
    env: Env = field(default_factory=Env)
    budget: Budget = field(default_factory=Budget)

    def compile(self, directory: Path):
        """Write an inspectable standard Harbor task; does not launch resources."""
        from .backends.runtime import compile_task

        return compile_task(self, directory)

    async def run(
        self,
        *,
        directory: Path,
        backend="docker",
        trials=1,
        parallel=1,
        resume_from=None,
        host=None,
    ):
        """Run trials, optionally restoring one saved logical-state checkpoint."""
        if host is not None:
            from .hosts import Host

            if not isinstance(host, Host):
                raise TypeError("host must be Host or None (current process)")
            return await host.run(
                self,
                directory=directory,
                backend=backend,
                trials=trials,
                parallel=parallel,
                resume_from=resume_from,
            )
        from .backends.runtime import run

        return await run(
            self,
            directory=directory,
            backend=backend,
            trials=trials,
            parallel=parallel,
            resume_from=resume_from,
        )

    def start(self, *, directory: Path, backend="docker", resume_from=None):
        """Start one controllable live trial; use its async context for cleanup."""
        from .live import LiveRun

        return LiveRun(
            self, directory=directory, backend=backend, resume_from=resume_from
        )
