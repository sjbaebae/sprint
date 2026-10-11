"""Definitions describe intent; backend-specific configuration stays out of tasks."""

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Sandbox:
    """Reusable execution image, Python tooling and network policy."""

    image: str = "python:3.12-slim"
    packages: tuple[str, ...] = ()
    internet: str = "none"
    allowed_hosts: tuple[str, ...] = ()

    def __post_init__(self):
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
    """Separate persistent sandbox for submitted Python jobs within one trial."""

    sandbox: Sandbox = field(default_factory=Sandbox)
    resources: Resources = field(default_factory=Resources)
    seconds: int = 600
    lifetime_seconds: int = 3600

    def __post_init__(self):
        if type(self.lifetime_seconds) is not int or self.lifetime_seconds < 1:
            raise ValueError("worker lifetime must be a positive integer")
        if type(self.seconds) is not int or self.seconds < 1:
            raise ValueError("worker timeout must be a positive integer")


@dataclass(frozen=True)
class World:
    """Shared workspace and sandbox; reusable across tasks and evaluations.

    Files are copied into the learner workspace. Never put protected data here.
    Named Files are read-only in the learner and workers; Worker attachments
    expose code submission and checkpoint collection through the host broker.
    """

    sandbox: Sandbox = field(default_factory=Sandbox)
    files: Path | None = None
    resources: Resources = field(default_factory=Resources)
    attachments: dict[str, Files | Worker] = field(default_factory=dict)

    def __post_init__(self):
        for name, attachment in self.attachments.items():
            if name == "control" or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]*", name):
                raise ValueError("attachment names must be simple identifiers")
            if not isinstance(attachment, (Files, Worker)):
                raise TypeError("attachments must be Files or Worker")


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

    def __post_init__(self):
        for name in (self.entrypoint, self.rubric):
            if name is not None and (
                Path(name).is_absolute() or ".." in Path(name).parts
            ):
                raise ValueError("validation paths must remain inside the bundle")
        if (
            len(self.weights) != 2
            or any(not math.isfinite(x) or x < 0 for x in self.weights)
            or not math.isclose(sum(self.weights), 1)
        ):
            raise ValueError(
                "verifier and judge weights must be nonnegative and sum to one"
            )
        if self.weights[1] and not self.rubric:
            raise ValueError("model-based grading requires a rubric")
        if self.seconds <= 0:
            raise ValueError("validation timeout must be positive")


@dataclass(frozen=True)
class Agent:
    """The solver model and its harness, independent of the task's target model."""

    model: str
    harness: str = "codex"
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Budget:
    """Trial wall time. Harness-specific action limits require a capable adapter."""

    seconds: int = 3600

    def __post_init__(self):
        if self.seconds <= 0:
            raise ValueError("trial timeout must be positive")


@dataclass(frozen=True)
class Experiment:
    """Combine independent definitions, then compile or execute on a backend."""

    task: Task
    validation: Validation
    agent: Agent
    world: World = field(default_factory=World)
    budget: Budget = field(default_factory=Budget)

    def compile(self, directory: Path):
        """Write an inspectable standard Harbor task; does not launch resources."""
        from .backends.harbor import compile_task

        return compile_task(self, directory)

    async def run(self, *, directory: Path, backend="docker", trials=1, parallel=1):
        """Run fresh trials through the selected infrastructure backend."""
        from .backends.harbor import run

        return await run(
            self, directory=directory, backend=backend, trials=trials, parallel=parallel
        )
