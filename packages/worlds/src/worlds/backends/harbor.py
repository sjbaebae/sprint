"""Translate the small authoring API into native Harbor tasks and jobs."""

import json
import shlex
import shutil
from pathlib import Path


def copy_tree(source, target):
    """Reject symlinks and special files; never accidentally include outside data."""
    source = Path(source)
    if source.is_symlink() or not source.is_dir():
        raise ValueError("bundle must be a real directory")
    for path in source.rglob("*"):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError(f"bundle contains a symlink or special file: {path}")
    shutil.copytree(source, target)


def dockerfile(
    sandbox,
    *,
    verifier=False,
    copy_workspace=True,
    submission="/app/submission",
    attachments=(),
):
    """Build tooling once in the image; do not download dependencies during grading."""
    lines = [f"FROM {sandbox.image}", "WORKDIR /app"]
    if sandbox.packages:
        lines.append(
            "RUN python -m pip install --no-cache-dir " + shlex.join(sandbox.packages)
        )
    if verifier:
        lines += ["COPY . /tests"]
    else:
        lines.append("RUN useradd --create-home agent")
        if copy_workspace:
            lines.append("COPY workspace/ /app/")
        # Explicit RUN works with providers whose Dockerfile translation drops
        # COPY --chown. Create the output directory before dropping privileges.
        lines.append(
            f"RUN mkdir -p {shlex.quote(submission)} && chown -R agent:agent /app"
        )
        for name in attachments:
            lines.append(f"COPY resources/{name}/ /resources/{name}/")
        if attachments:
            lines.append(
                "RUN chown -R root:root /resources && "
                "find /resources -type d -exec chmod 555 {} + && "
                "find /resources -type f -exec chmod 444 {} +"
            )
        lines.append("USER agent")
    return "\n".join(lines) + "\n"


def compile_task(experiment, directory):
    """Emit a new task with separate verifier; refuse to overwrite authored state."""
    import toml
    from ..spec import Files, Worker
    from harbor.models.task.config import TaskConfig

    directory = Path(directory)
    if experiment.world.files is not None:
        public = Path(experiment.world.files).resolve()
        private = Path(experiment.validation.bundle).resolve()
        if public.is_relative_to(private) or private.is_relative_to(public):
            raise ValueError(
                "learner files and private validation bundle must be separate trees"
            )
        if directory.resolve().is_relative_to(public):
            raise ValueError("generated task cannot be inside the learner source tree")
    if directory.resolve().is_relative_to(Path(experiment.validation.bundle).resolve()):
        raise ValueError("generated task cannot be inside the validation source tree")
    private = Path(experiment.validation.bundle).resolve()
    for attachment in experiment.world.attachments.values():
        if not isinstance(attachment, Files):
            continue
        source = Path(attachment.path).resolve()
        if source.is_relative_to(private) or private.is_relative_to(source):
            raise ValueError(
                "attachments and private validation must be separate trees"
            )
        if directory.resolve().is_relative_to(source):
            raise ValueError(
                "generated task cannot be inside an attachment source tree"
            )
    directory.mkdir(parents=True, exist_ok=False)
    directory.chmod(0o700)
    learner = directory / "environment"
    tests = directory / "tests"
    learner.mkdir()
    tests.mkdir()
    world = experiment.world
    validation = experiment.validation
    if world.files is None:
        (learner / "workspace").mkdir()
    else:
        copy_tree(world.files, learner / "workspace")
    for name, attachment in world.attachments.items():
        if isinstance(attachment, Worker):
            worker_dir = directory / "workers" / name
            worker_dir.mkdir(parents=True)
            (worker_dir / "Dockerfile").write_text(
                dockerfile(attachment.sandbox, copy_workspace=False)
            )
            continue
        (learner / "resources").mkdir(exist_ok=True)
        copy_tree(attachment.path, learner / "resources" / name)
    copy_tree(validation.bundle, tests / "bundle")
    if not (tests / "bundle" / validation.entrypoint).is_file():
        raise ValueError("validation entrypoint is missing")
    if validation.rubric and not (tests / "bundle" / validation.rubric).is_file():
        raise ValueError("rubric is missing")
    (learner / "Dockerfile").write_text(
        dockerfile(
            world.sandbox,
            copy_workspace=any((learner / "workspace").rglob("*")),
            submission=experiment.task.submission,
            attachments=[
                name
                for name, value in world.attachments.items()
                if isinstance(value, Files)
            ],
        )
    )
    (tests / "Dockerfile").write_text(dockerfile(validation.sandbox, verifier=True))
    shutil.copyfile(Path(__file__).parents[1] / "validation.py", tests / "validate.py")
    (tests / "config.json").write_text(
        json.dumps(
            {
                "entrypoint": validation.entrypoint,
                "rubric": validation.rubric,
                "weights": validation.weights,
                "submission": experiment.task.submission,
            }
        )
    )
    (tests / "test.sh").write_text(
        "#!/bin/sh\nset -eu\npython /tests/validate.py /tests/config.json\n"
    )
    (tests / "test.sh").chmod(0o755)
    if validation.reference is not None:
        reference = Path(validation.reference)
        if reference.is_symlink() or not reference.is_file():
            raise ValueError("reference solution must be a regular shell script")
        solution = directory / "solution"
        solution.mkdir()
        shutil.copyfile(reference, solution / "solve.sh")
        (solution / "solve.sh").chmod(0o755)
    (directory / "instruction.md").write_text(
        experiment.task.objective
        + "".join(
            f"\nRead-only resource {name}: /resources/{name}\n"
            for name, value in world.attachments.items()
            if isinstance(value, Files)
        )
        + f"\n\nWrite your final artifact under {experiment.task.submission}.\n"
    )

    def policy(sandbox):
        mode = {"none": "no-network", "full": "public", "allowlist": "allowlist"}[
            sandbox.internet
        ]
        return {"network_mode": mode, "allowed_hosts": list(sandbox.allowed_hosts)}

    def resources(request):
        return {
            "cpus": request.cpus,
            "memory_mb": request.memory_mb,
            "gpus": request.gpu_count if request.gpu else 0,
            "gpu_types": [request.gpu] if request.gpu else [],
        }

    sandbox = world.sandbox
    config = {
        "schema_version": "1.4",
        "task": {"name": "worlds/" + experiment.task.name, "version": "0.1.0"},
        "artifacts": [experiment.task.submission],
        "agent": {
            "timeout_sec": experiment.budget.seconds,
            "user": "agent",
            **policy(sandbox),
        },
        "environment": {
            **resources(world.resources),
            **policy(sandbox),
        },
        "verifier": {
            "timeout_sec": validation.seconds,
            "environment_mode": "separate",
            "include_agent_trace": True,
            "user": "root",
            "environment": {
                **resources(validation.resources),
                **policy(validation.sandbox),
            },
            **policy(validation.sandbox),
        },
    }
    TaskConfig.model_validate(config)
    (directory / "task.toml").write_text(toml.dumps(config))
    return directory


async def run(experiment, *, directory, backend, trials, parallel):
    """Use Harbor scheduling and native agent adapters rather than another runner."""
    from harbor import (
        Job,
        JobConfig,
        TrialAgentConfig,
        TrialEnvironmentConfig,
        TrialTaskConfig,
    )

    from ..spec import Worker

    if any(
        isinstance(value, Worker) for value in experiment.world.attachments.values()
    ):
        raise NotImplementedError(
            "worker attachments require the pending agent queue adapter; use WorkerSession for operator tests"
        )
    if trials < 1 or parallel < 1:
        raise ValueError("trials and parallel must be positive")
    directory = Path(directory).resolve()
    task = compile_task(experiment, directory / "task")
    config = JobConfig(
        jobs_dir=directory / "runs",
        n_attempts=trials,
        n_concurrent_trials=parallel,
        tasks=[TrialTaskConfig(path=task)],
        agents=[
            TrialAgentConfig(
                name=experiment.agent.harness,
                model_name=experiment.agent.model,
                kwargs=experiment.agent.options,
            )
        ],
        environment=TrialEnvironmentConfig(type=backend),
    )
    from harbor.trial.hooks import TrialEvent
    from ..results import collect

    job = await Job.create(config)
    events_path = directory / "events.jsonl"

    async def record(event):
        # Operator-side lifecycle times; not substituted for absent agent turn times.
        payload = {
            "timestamp": event.timestamp.isoformat(),
            "event": event.event.value,
            "trial": event.trial_name,
        }
        with events_path.open("a") as stream:
            stream.write(json.dumps(payload) + "\n")

    for event in TrialEvent:
        job.add_hook(event, record)
    native = await job.run()
    return collect(
        directory, job.job_dir, native, submission=experiment.task.submission
    )
