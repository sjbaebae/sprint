"""Translate the small authoring API into native Harbor tasks and jobs."""

import contextlib
import json
import math
import os
import re
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


def harness_setup(agent, sandbox):
    """Prepare supported CLIs in the default image, independently of task tools.

    Custom images retain adapter installation behavior. No credentials belong in
    these build steps; authentication is injected only when the trial starts.
    """
    packages = {
        "codex": "@openai/codex",
        "bounded-codex": "@openai/codex",
        "claude-code": "@anthropic-ai/claude-code",
    }
    package = packages.get(agent.harness) if agent is not None else None
    if package is None or sandbox.image != "python:3.12-slim":
        return ()
    version = agent.options.get("version")
    if version:
        package += "@" + str(version)
    return (
        "apt-get update && apt-get install -y --no-install-recommends "
        "bash curl ca-certificates git nodejs npm procps ripgrep "
        "&& rm -rf /var/lib/apt/lists/*",
        "npm install -g " + shlex.quote(package),
    )


def dockerfile(
    sandbox,
    *,
    verifier=False,
    copy_workspace=True,
    submission="/app/submission",
    attachments=(),
    workers=(),
    managed_codex=False,
    agent=None,
):
    """Build tooling once in the image; do not download dependencies during grading."""
    lines = [f"FROM {sandbox.image}", "WORKDIR /app"]
    lines.extend("RUN " + command for command in harness_setup(agent, sandbox))
    lines.extend("RUN " + command for command in sandbox.setup)
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
        if workers:
            lines += [
                "COPY resource_client.py /opt/sprint-resource.py",
                "RUN mkdir -p /resources/control/requests /resources/control/results && "
                "chown agent:agent /resources/control/requests && "
                "chmod 755 /resources /resources/control /resources/control/results && "
                "printf '#!/bin/sh\\nexec python -I -S /opt/sprint-resource.py \"$@\"\\n' > /usr/local/bin/sprint-resource && "
                "chmod 755 /usr/local/bin/sprint-resource && chmod 444 /opt/sprint-resource.py",
                "COPY resource_manifest.json /resources/control/manifest.json",
            ]
        if managed_codex:
            lines += [
                "COPY codex-requirements.toml /etc/codex/requirements.toml",
                "RUN chown root:root /etc/codex /etc/codex/requirements.toml && "
                "chmod 755 /etc/codex && chmod 444 /etc/codex/requirements.toml",
            ]
        lines.append("USER agent")
    return "\n".join(lines) + "\n"


def build_worker(worker, directory, files=None):
    """One build definition for inspected and provisioned worker images."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for name, attachment in (files or {}).items():
        (directory / "resources").mkdir(exist_ok=True)
        copy_tree(attachment.path, directory / "resources" / name)
    (directory / "Dockerfile").write_text(
        dockerfile(
            worker.sandbox, copy_workspace=False, attachments=tuple(files or {})
        ).replace(
            "USER agent\n",
            "COPY worker_export.py /opt/sprint-export.py\n"
            "COPY worker_run.py /opt/sprint-run.py\n"
            "RUN chmod 444 /opt/sprint-export.py /opt/sprint-run.py\nUSER root\n",
        )
    )
    shutil.copyfile(
        Path(__file__).parents[1] / "worker_export.py", directory / "worker_export.py"
    )
    shutil.copyfile(
        Path(__file__).parents[1] / "worker_run.py", directory / "worker_run.py"
    )
    return directory


def codex_requirements(agent, sandbox):
    """Generate the bounded harness policy; backend networking remains authoritative."""
    profile = agent.options.get("permission_profile", "offline")
    if not isinstance(profile, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", profile):
        raise ValueError("invalid managed permission profile")
    return {
        "allowed_approval_policies": ["never"],
        "allowed_web_search_modes": (
            ["disabled", "cached", "live"]
            if sandbox.internet == "full"
            else ["disabled"]
        ),
        "allowed_permission_profiles": {profile: True},
        "default_permissions": profile,
        "permissions": {
            profile: {
                "filesystem": {":root": "write"},
                "network": {"enabled": sandbox.internet != "none"},
            }
        },
        "features": {
            "multi_agent": False,
            "multi_agent_v2": False,
            "plugins": False,
            "remote_plugin": False,
            "apps": False,
            "browser_use": False,
            "computer_use": False,
        },
    }


def compile_task(experiment, directory):
    """Emit a new task with separate verifier; refuse to overwrite authored state."""
    import toml
    from ..spec import Files, Worker
    from sprint.models.task.config import TaskConfig

    directory = Path(directory)
    managed = (
        codex_requirements(experiment.agent, experiment.env.sandbox)
        if experiment.agent.harness == "bounded-codex"
        else None
    )
    if experiment.env.files is not None:
        public = Path(experiment.env.files).resolve()
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
    for attachment in experiment.env.attachments.values():
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
    world = experiment.env
    validation = experiment.validation
    if world.files is None:
        (learner / "workspace").mkdir()
    else:
        copy_tree(world.files, learner / "workspace")
    for name, attachment in world.attachments.items():
        if isinstance(attachment, Worker):
            worker_dir = directory / "workers" / name
            build_worker(
                attachment,
                worker_dir,
                {
                    key: value
                    for key, value in world.attachments.items()
                    if isinstance(value, Files)
                },
            )
            continue
        (learner / "resources").mkdir(exist_ok=True)
        copy_tree(attachment.path, learner / "resources" / name)
    copy_tree(validation.bundle, tests / "bundle")
    if not (tests / "bundle" / validation.entrypoint).is_file():
        raise ValueError("validation entrypoint is missing")
    if validation.rubric and not (tests / "bundle" / validation.rubric).is_file():
        raise ValueError("rubric is missing")
    if managed is not None:
        (learner / "codex-requirements.toml").write_text(toml.dumps(managed))
    workers = {
        name: value
        for name, value in world.attachments.items()
        if isinstance(value, Worker)
    }
    if workers:
        shutil.copyfile(
            Path(__file__).parents[1] / "resource_client.py",
            learner / "resource_client.py",
        )
        (learner / "resource_manifest.json").write_text(
            json.dumps(
                {
                    name: {
                        "operations": ["run", "upload", "status", "collect"]
                        + (["exec"] if value.shell else [])
                        + (
                            ["connect"]
                            if value.shell
                            and value.sandbox.internet != "none"
                            and value.backend in {None, "modal"}
                            else []
                        ),
                        "seconds": value.seconds,
                        "gpu": value.resources.gpu,
                    }
                    for name, value in workers.items()
                }
            )
        )
    (learner / "Dockerfile").write_text(
        dockerfile(
            world.sandbox,
            agent=experiment.agent,
            managed_codex=managed is not None,
            copy_workspace=any((learner / "workspace").rglob("*")),
            workers=workers,
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
        + (
            "\nUse sprint-resource list; sprint-resource upload NAME LOCAL_FILE RELATIVE_DESTINATION --wait 60 (at most 1 MiB); sprint-resource run NAME SCRIPT.py --wait 60; sprint-resource status REQUEST_ID --wait 60; sprint-resource collect REQUEST_ID FILE_RELATIVE_TO_APP --wait 60. Collection returns a read-only artifact path to copy into your submission. Jobs execute Python on the named worker and preserve /app between calls.\n"
            if workers
            else ""
        )
        + (
            "\nWorkers that list exec permit sprint-resource exec NAME 'SHELL COMMAND' --wait 60. Shell commands preserve background processes, including after an ordinary command timeout (exit 124); redirect output to files. Lifetime expiry, cancellation or provider failure closes the worker.\n"
            if any(worker.shell for worker in workers.values())
            else ""
        )
        + (
            "\nWorkers that list connect provide ordinary SSH/SCP: sprint-resource connect NAME --wait 60 returns a pinned ssh_config path. Use ssh -F CONFIG gpu 'COMMAND' or scp -F CONFIG LOCAL_FILE gpu:/app/REMOTE_FILE.\n"
            if any(
                worker.shell and worker.sandbox.internet != "none"
                for worker in workers.values()
            )
            else ""
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
        "task": {"name": "sprint/" + experiment.task.name, "version": "0.1.0"},
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


def agent_auth_env(agent):
    """Select API billing explicitly without persisting keys in author definitions."""
    if agent.auth == "auto":
        return {}
    name = (
        "OPENAI_API_KEY"
        if agent.harness in {"codex", "bounded-codex"}
        else "ANTHROPIC_API_KEY"
    )
    key = os.environ.get(name, "").strip()
    if not key:
        raise ValueError(f"{name} must be set on the host for API-key authentication")
    if agent.harness in {"codex", "bounded-codex"}:
        return {name: key, "CODEX_AUTH_JSON_PATH": "", "CODEX_FORCE_AUTH_JSON": "0"}
    return {name: key, "CLAUDE_CODE_OAUTH_TOKEN": "", "CLAUDE_FORCE_OAUTH": "0"}


async def restore_workers(broker, checkpoint, records):
    """Restore logical worker state and request lineage before polling resumes."""
    import math
    from dataclasses import replace
    from ..state_transfer import restore_tree
    from ..workers import WorkerSession

    broker.seen = set(checkpoint.metadata["broker"]["seen"])
    broker.completed = dict(checkpoint.metadata["broker"]["completed"])
    broker.reservations = dict(checkpoint.metadata["broker"].get("reservations", {}))
    if records.is_dir():
        for item in records.iterdir():
            if item.is_file():
                shutil.copyfile(item, broker.directory / item.name)
    for name, state in checkpoint.metadata["workers"].items():
        if name not in broker.workers:
            raise ValueError("checkpoint references an ungranted worker")
        if state.get("closed") is True:
            session = WorkerSession(
                broker.workers[name],
                directory=broker.directory / name,
                backend=broker.backend,
                context=broker.contexts[name],
            )
            await session.__aenter__()
            await session.close()
            broker.sessions[name] = session
            previous = records / name
            if previous.is_dir():
                shutil.copytree(previous, session.directory, dirs_exist_ok=True)
            continue
        remaining = state["remaining_seconds"]
        if not math.isfinite(remaining) or remaining <= 0:
            raise ValueError("checkpoint worker budget is exhausted")
        session = WorkerSession(
            replace(broker.workers[name], lifetime_seconds=math.ceil(remaining)),
            directory=broker.directory / name,
            backend=broker.backend,
            context=broker.contexts[name],
            learner_ssh_public_key=broker.ssh_key(name),
        )
        await session.__aenter__()
        broker.sessions[name] = session
        previous = records / name
        if previous.is_dir():
            shutil.copytree(previous, session.directory, dirs_exist_ok=True)
        await session.start()
        await restore_tree(
            session.environment,
            checkpoint,
            "worker-" + name,
            session.worker.state_paths,
        )
        session.record("worker-restored", parent=str(checkpoint.path))
        if session.learner_ssh_public_key:
            await broker.connect(name, session)


async def run(
    experiment,
    *,
    directory,
    backend,
    trials,
    parallel,
    resume_from=None,
    control=None,
    compiled_task=None,
):
    """Use Harbor scheduling and native agent adapters rather than another runner."""
    from sprint import (
        Job,
        JobConfig,
        TrialAgentConfig,
        TrialEnvironmentConfig,
        TrialVerifierConfig,
        TrialTaskConfig,
    )

    from ..spec import Worker

    if trials < 1 or parallel < 1:
        raise ValueError("trials and parallel must be positive")
    if experiment.budget.worker_spend is not None:
        for attachment in experiment.env.attachments.values():
            if isinstance(attachment, Worker) and attachment.hourly_cost is None:
                raise ValueError("spending limit requires hourly_cost on every worker")
    if backend == "allocated":
        from sprint.environments.allocated import allocated_config

        _, workspace, _ = allocated_config()
        if parallel != 1 or experiment.validation.backend is None:
            raise ValueError(
                "allocated learner requires parallel=1 and an explicit validation backend"
            )
        if experiment.agent.harness not in {"bounded-codex", "oracle", "nop"}:
            raise ValueError("allocated learner requires bounded-codex, oracle, or nop")
        if experiment.env.sandbox.internet != "full":
            raise ValueError("allocated learner supports full internet only")
        if (
            experiment.env.sandbox.setup
            or experiment.env.sandbox.packages
            or experiment.env.sandbox.image != "python:3.12-slim"
        ):
            raise ValueError("allocated tooling must be preinstalled by the operator")
        if any(
            value.backend in {None, "allocated"}
            for value in experiment.env.attachments.values()
            if isinstance(value, Worker)
        ):
            raise ValueError(
                "allocated learner workers require an explicit container backend"
            )
        allowed_roots = (
            workspace,
            Path("/home/agent"),
            Path("/logs/agent"),
            Path("/resources/control"),
            Path("/installed-agent"),
        )
        if any(
            not any(Path(root).is_relative_to(allowed) for allowed in allowed_roots)
            for root in experiment.env.state_paths
        ):
            raise ValueError(
                "allocated state_paths must stay inside the workspace or learner harness directories"
            )
    directory = Path(directory).resolve()
    auth_env = agent_auth_env(experiment.agent)
    checkpoint, trajectory, seconds = None, None, experiment.budget.seconds
    if resume_from is not None:
        if trials != 1 or parallel != 1:
            raise ValueError(
                "restore requires one trial; fork checkpoints into separate runs"
            )
        from ..live import prepare_restore

        checkpoint, task, trajectory, seconds = prepare_restore(
            experiment, resume_from, directory
        )
    elif compiled_task is not None:
        task = Path(compiled_task).resolve(strict=True)
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        directory.chmod(0o700)
    else:
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        directory.chmod(0o700)
        task = compile_task(experiment, directory / "task")
    import socket

    (directory / "placement.json").write_text(
        json.dumps(
            {
                "host": {"hostname": socket.gethostname(), "pid": os.getpid()},
                "learner_backend": backend,
                "verifier_backend": experiment.validation.backend or backend,
                "workers": {
                    name: value.backend or backend
                    for name, value in experiment.env.attachments.items()
                    if isinstance(value, Worker)
                },
            },
            indent=2,
        )
    )
    agent_options = dict(experiment.agent.options)
    if experiment.agent.harness == "bounded-codex":
        agent_options["seconds"] = seconds
        agent_options["actions_used"] = (
            checkpoint.metadata["actions_used"] if checkpoint else 0
        )
    config = JobConfig(
        jobs_dir=directory / "runs",
        n_attempts=trials,
        n_concurrent_trials=parallel,
        tasks=[TrialTaskConfig(path=task)],
        agents=[
            TrialAgentConfig(
                name=experiment.agent.harness,
                model_name=experiment.agent.model,
                kwargs=agent_options,
                env=auth_env,
                load_trajectory=trajectory,
                override_timeout_sec=seconds,
            )
        ],
        verifier=TrialVerifierConfig(
            environment=TrialEnvironmentConfig(
                type=experiment.validation.backend,
                kwargs={"sandbox_timeout_secs": experiment.validation.seconds + 300}
                if experiment.validation.backend == "modal"
                else {},
            )
            if experiment.validation.backend is not None
            else None,
        ),
        environment=TrialEnvironmentConfig(
            type=backend,
            kwargs={
                "sandbox_timeout_secs": math.ceil(
                    seconds + experiment.validation.seconds + 300
                )
            }
            if backend == "modal"
            else {},
        ),
    )
    from sprint.trial.hooks import TrialEvent
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
    from ..broker import ResourceBroker, finalize_brokers

    brokers = {}

    async def resources(event):
        if event.event == TrialEvent.AGENT_START:
            if checkpoint is not None:
                from ..state_transfer import restore_tree

                await restore_tree(
                    event.environment, checkpoint, "learner", experiment.env.state_paths
                )
            broker = None
            workers = {
                name: value
                for name, value in experiment.env.attachments.items()
                if isinstance(value, Worker)
            }
            if workers:
                broker = ResourceBroker(
                    event.environment,
                    workers,
                    directory=directory / "resources" / event.trial_name,
                    backend=backend,
                    contexts={name: task / "workers" / name for name in workers},
                    budget=experiment.budget,
                )
                brokers[event.trial_name] = broker
                if checkpoint is not None:
                    await broker.gate.acquire()
                await broker.start()
                if broker.task is not None:
                    event.services.append(broker.task)
                if checkpoint is not None:
                    try:
                        await restore_workers(
                            broker, checkpoint, directory / "resume-broker"
                        )
                    finally:
                        broker.gate.release()
            if control is not None:
                control.attached(
                    event.environment, broker, task, seconds=seconds, parent=checkpoint
                )
        elif event.event == TrialEvent.AGENT_END:
            if control is not None:
                await control.leaving()
            broker = brokers.get(event.trial_name)
            if broker is not None:
                try:
                    await broker.close()
                finally:
                    # Retain failed deletion handles for a final bounded retry.
                    # close() consumes service errors once, independently of deletion.
                    if all(s.environment is None for s in broker.sessions.values()):
                        brokers.pop(event.trial_name, None)

    async def evidence(event):
        # Separate-mode hook runs after learner shutdown and log collection.
        # Replace learner-created lookalikes before supplying operator evidence.
        if event.environment is None:
            raise RuntimeError("missing trial environment for evidence transfer")
        target = event.environment.trial_paths.agent_dir / "sprint-resources"
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        elif target.exists() or target.is_symlink():
            target.unlink()
        target.mkdir(mode=0o700)
        source = directory / "resources" / event.trial_name
        if source.is_dir():
            for worker in source.iterdir():
                if worker.is_dir():
                    destination = target / worker.name
                    destination.mkdir()
                    for file in worker.iterdir():
                        if file.is_file() and file.suffix in {".py", ".jsonl"}:
                            shutil.copyfile(file, destination / file.name)
                elif worker.suffix in {".json", ".jsonl"}:
                    shutil.copyfile(worker, target / worker.name)

    job.add_hook(TrialEvent.VERIFICATION_START, evidence)
    job.add_hook(TrialEvent.AGENT_START, resources)
    job.add_hook(TrialEvent.AGENT_END, resources)
    try:
        try:
            native = await job.run()
        finally:
            await finalize_brokers(brokers, directory)
    except BaseException as error:
        from ..results import collect_interrupted

        try:
            collect_interrupted(
                directory, job.job_dir, error, submission=experiment.task.submission
            )
        except Exception as recording_error:
            # A review failure must not replace the original cancellation/error.
            with contextlib.suppress(OSError):
                (directory / "interruption-recording-error.json").write_text(
                    json.dumps(
                        {
                            "exception_type": type(recording_error).__name__,
                            "message": str(recording_error),
                        }
                    )
                )
        raise
    return collect(
        directory, job.job_dir, native, submission=experiment.task.submission
    )
