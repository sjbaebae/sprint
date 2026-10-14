"""Checkpoint/resume logical RL runs without saving process or GPU memory."""

import asyncio
import hashlib
import json
import math
import re
import shlex
import shutil
import tarfile
import tempfile
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .checkpoint_store import Checkpoint, CheckpointStore


def definition(experiment):
    """Pin task/configuration; auth values are resolved separately at runtime."""
    return json.loads(json.dumps(asdict(experiment), default=str, sort_keys=True))


async def remote_helper(environment):
    remote = "/.sprint-state-" + uuid4().hex + ".py"
    await environment.upload_file(Path(__file__).with_name("remote_state.py"), remote)
    return remote


async def checked(environment, command, *, timeout=600):
    result = await environment.exec(command, user="root", timeout_sec=timeout)
    if result.return_code != 0:
        raise RuntimeError("state transfer failed: " + str(result.stderr))
    return result.stdout or ""


async def restore_tree(environment, checkpoint, tree, roots):
    """Restore approved mutable roots, preserving deletions and file metadata."""
    entries = checkpoint.metadata["trees"][tree]
    prefixes = tuple(root.lstrip("/") for root in roots)
    for name in entries:
        if not any(name == p or name.startswith(p + "/") for p in prefixes):
            raise ValueError("checkpoint state escapes its declared roots")
    with tempfile.TemporaryDirectory(prefix="sprint-restore-") as temporary:
        archive = Path(temporary) / "state.tar"
        checkpoint.store.export_archive(entries, archive)
        remote = "/.sprint-restore-" + uuid4().hex + ".tar"
        await environment.upload_file(archive, remote)
        helper = await remote_helper(environment)
        try:
            await checked(
                environment,
                f"python -I -S {shlex.quote(helper)} validate-roots {shlex.quote(json.dumps(roots))}",
            )
            # Recreate roots from the saved manifest, so deleted base files stay deleted.
            remove = " ".join(shlex.quote(root) for root in roots)
            await checked(
                environment,
                f"rm -rf -- {remove} && tar -xpf {shlex.quote(remote)} -C /",
            )
        finally:
            await checked(
                environment, "rm -f -- " + shlex.join([remote, helper]), timeout=30
            )


def validate_resume_limits(experiment, metadata):
    """Reject inconsistent budgets and lineage before provisioning any sandbox."""
    from .spec import Worker

    if experiment.agent.harness == "bounded-codex":
        maximum = experiment.agent.options.get("max_turns", 200)
        used = metadata.get("actions_used")
        if (
            type(maximum) is not int
            or not 1 <= maximum <= 200
            or type(used) is not int
            or not 0 <= used < maximum
        ):
            raise ValueError("checkpoint action budget is invalid or exhausted")

    remaining = metadata.get("remaining_seconds")
    if (
        type(remaining) not in (int, float)
        or not math.isfinite(remaining)
        or not 0 < remaining <= experiment.budget.seconds
    ):
        raise ValueError(
            "checkpoint remaining trial budget exceeds its limits or is exhausted"
        )
    workers = {
        name: value
        for name, value in experiment.env.attachments.items()
        if isinstance(value, Worker)
    }
    states = metadata.get("workers", {})
    broker = metadata.get("broker", {})
    if not isinstance(states, dict) or not isinstance(broker, dict):
        raise ValueError("invalid checkpoint worker or broker state")
    for name, state in states.items():
        if name not in workers or not isinstance(state, dict):
            raise ValueError("checkpoint references an ungranted worker")
        if set(state) == {"closed"} and state["closed"] is True:
            continue
        if set(state) != {"remaining_seconds"}:
            raise ValueError("invalid checkpoint worker state fields")
        seconds = state.get("remaining_seconds")
        if (
            type(seconds) not in (int, float)
            or not math.isfinite(seconds)
            or not 0 < seconds <= workers[name].lifetime_seconds
        ):
            raise ValueError(
                "checkpoint worker budget exceeds its limits or is exhausted"
            )
    seen = broker.get("seen", [])
    completed = broker.get("completed", {})
    reservations = broker.get("reservations", {})
    if (
        not isinstance(seen, list)
        or any(
            not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{32}", key)
            for key in seen
        )
        or len(seen) != len(set(seen))
        or len(seen) > experiment.budget.resource_requests
    ):
        raise ValueError("invalid checkpoint request lineage")
    if not isinstance(completed, dict) or any(
        key not in seen or name not in states for key, name in completed.items()
    ):
        raise ValueError("invalid checkpoint completed request lineage")
    if not isinstance(reservations, dict):
        raise ValueError("invalid checkpoint spending reservations")
    for name, amount in reservations.items():
        if (
            name not in workers
            or type(amount) not in (int, float)
            or not math.isfinite(amount)
            or amount < 0
        ):
            raise ValueError("invalid checkpoint spending reservation")
        expected = (
            (workers[name].hourly_cost or 0) * workers[name].lifetime_seconds / 3600
        )
        if not math.isclose(amount, expected, rel_tol=1e-12, abs_tol=1e-12):
            raise ValueError(
                "checkpoint spending reservation differs from declared lifetime cost"
            )
    limit = experiment.budget.worker_spend
    if limit is not None and (
        set(states) - set(reservations) or sum(reservations.values()) > limit
    ):
        raise ValueError("checkpoint spending limit exceeded or reservation missing")


def validate_resource_runtime(experiment, checkpoint):
    """Reject mixed host/container helper versions before spending on restore.

    A compiled checkpoint retains its original immutable image inputs. Queue and
    worker protocols must match the calling runtime; silently replacing helpers
    would change the captured release. Use its original Sprint build to restore
    an incompatible checkpoint.
    """
    from .spec import Worker

    workers = {
        name
        for name, value in experiment.env.attachments.items()
        if isinstance(value, Worker)
    }
    if not workers:
        return
    release = checkpoint.metadata["trees"].get("release", {})
    expected = {"release/environment/resource_client.py": "resource_client.py"}
    for name in workers:
        for helper in ("worker_run.py", "worker_export.py"):
            expected[f"release/workers/{name}/{helper}"] = helper
    for name, helper in expected.items():
        entry = release.get(name)
        checksum = hashlib.sha256(
            Path(__file__).with_name(helper).read_bytes()
        ).hexdigest()
        if (
            not isinstance(entry, dict)
            or entry.get("kind") != "file"
            or entry.get("sha256") != checksum
        ):
            raise ValueError(
                f"checkpoint resource runtime differs: {name}; "
                "resume using the Sprint build that created this checkpoint"
            )


def prepare_restore(experiment, source, directory):
    """Validate state and materialize the exact compiled release before launch."""
    checkpoint = source if isinstance(source, Checkpoint) else Checkpoint(source)
    checkpoint.verify()
    metadata = checkpoint.metadata
    if metadata["definition"] != definition(experiment):
        raise ValueError("checkpoint belongs to a different experiment definition")
    validate_resume_limits(experiment, metadata)
    validate_resource_runtime(experiment, checkpoint)
    from .spec import Worker

    roots_by_tree = {"learner": experiment.env.state_paths}
    for name, state in metadata.get("workers", {}).items():
        worker = experiment.env.attachments[name]
        if not isinstance(worker, Worker):
            raise ValueError("checkpoint references a non-worker attachment")
        if state.get("closed") is not True:
            roots_by_tree["worker-" + name] = worker.state_paths
    for tree, roots in roots_by_tree.items():
        entries = metadata["trees"].get(tree)
        if not isinstance(entries, dict):
            raise ValueError("checkpoint is missing a required state tree")
        prefixes = tuple(root.lstrip("/") for root in roots)
        if any(
            not any(
                name == prefix or name.startswith(prefix + "/") for prefix in prefixes
            )
            for name in entries
        ):
            raise ValueError("checkpoint state escapes its declared roots")
    remaining = metadata["remaining_seconds"]
    directory = Path(directory)
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    directory.chmod(0o700)
    task = directory / "task"
    if task.exists():
        raise FileExistsError(task)
    with tempfile.TemporaryDirectory(prefix="sprint-release-") as temporary:
        temporary = Path(temporary)
        archive = temporary / "release.tar"
        checkpoint.store.export_archive(metadata["trees"]["release"], archive)
        with tarfile.open(archive) as stream:
            # Compiled releases forbid external links; do not silently skip entries.
            stream.extractall(temporary / "extracted", filter="data")
        shutil.copytree(temporary / "extracted/release", task)
        broker_records = temporary / "extracted/broker"
        if broker_records.is_dir():
            shutil.copytree(broker_records, directory / "resume-broker")
    # Seed the harness from a complete native record, trimming only an interrupted
    # final write. Preserve its bytes in the checkpoint itself for inspection.
    trajectory = None
    native = metadata.get("native_session")
    if native:
        entry = metadata["trees"]["learner"][native]
        native_directory = directory / "resume-native"
        native_directory.mkdir(mode=0o700)
        target = native_directory / Path(native).name
        data = b"".join(checkpoint.store.get(key) for key in entry["chunks"])
        lines = []
        records = data.splitlines(keepends=True)
        for index, line in enumerate(records):
            try:
                json.loads(line)
            except (ValueError, UnicodeError):
                if index != len(records) - 1:
                    raise ValueError(
                        "corrupt native conversation inside checkpoint"
                    ) from None
                break
            lines.append(line.rstrip(b"\r\n") + b"\n")
        if not lines:
            raise ValueError("checkpoint has no complete native conversation records")
        target.write_bytes(b"".join(lines))
        target.chmod(0o600)
        trajectory = str(target)
    (directory / "resume.json").write_text(
        json.dumps(
            {
                "parent": str(checkpoint.path),
                "remaining_seconds": remaining,
                "recovery": "processes restarted; interrupted commands are not replayed by Sprint",
            },
            indent=2,
        )
    )
    return checkpoint, task, trajectory, remaining


class LiveRun:
    """Operator-controlled single trial with durable checkpoints and native resume.

    Files in declared state_paths, native logs, broker state and budgets survive.
    Processes, sockets and live GPU allocations do not. Applications must put
    their resumable state (including optimizer/RNG state) in checkpointed files.
    """

    def __init__(self, experiment, *, directory, backend, resume_from=None):
        self.experiment = experiment
        self.directory = Path(directory).resolve()
        self.backend = backend
        self.resume_from = resume_from
        self.task = None
        self.environment = None
        self.broker = None
        self.ready = asyncio.Event()
        self.lock = asyncio.Lock()
        self.started = None
        self.seconds = experiment.budget.seconds
        self.release = None
        self.parent = None
        self.active = False

    async def __aenter__(self):
        if self.experiment.agent.harness not in {
            "codex",
            "bounded-codex",
            "claude-code",
            "oracle",
            "nop",
        }:
            raise ValueError(
                "checkpointed runs currently support Codex, bounded Codex, Claude Code, oracle and nop"
            )
        from .backends.runtime import run

        self.task = asyncio.create_task(
            run(
                self.experiment,
                directory=self.directory,
                backend=self.backend,
                trials=1,
                parallel=1,
                resume_from=self.resume_from,
                control=self,
            )
        )
        waiter = asyncio.create_task(self.ready.wait())
        try:
            done, _ = await asyncio.wait(
                (self.task, waiter), return_when=asyncio.FIRST_COMPLETED
            )
            if self.task in done:
                await self.task
                raise RuntimeError(
                    "trial ended before a live environment became available"
                )
            return self
        except BaseException:
            await self.stop()
            raise
        finally:
            waiter.cancel()
            try:
                await waiter
            except asyncio.CancelledError:
                pass

    async def __aexit__(self, *_):
        await self.stop()

    async def wait(self):
        """Wait for the ordinary final validation and review records."""
        if self.task is None:
            raise RuntimeError("run has not started")
        return await self.task

    async def stop(self):
        """Stop the current branch; saved checkpoints remain restorable."""
        if self.task is not None and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

    def attached(self, environment, broker, release, *, seconds, parent=None):
        self.environment, self.broker, self.release = environment, broker, release
        self.seconds, self.parent = seconds, parent
        self.started = time.monotonic()
        self.active = True
        self.ready.set()

    async def leaving(self):
        # Block teardown while an export owns the state; mark expiration first so
        # an expiring trial cannot publish a falsely valid checkpoint.
        self.active = False
        async with self.lock:
            pass

    def status(self):
        """Inspect live limits without executing code in or changing the learner."""
        elapsed = time.monotonic() - self.started if self.started is not None else 0
        broker = self.broker
        return {
            "active": self.active,
            "remaining_seconds": max(0, self.seconds - elapsed),
            "resource_requests_used": len(broker.seen) if broker else 0,
            "resource_requests_limit": self.experiment.budget.resource_requests,
            "worker_concurrency": self.experiment.budget.worker_concurrency,
            "worker_spend_limit": self.experiment.budget.worker_spend,
            "worker_reservations": dict(broker.reservations) if broker else {},
            "workers": sorted(broker.sessions) if broker else [],
        }

    async def checkpoint(self, name):
        """Quiesce writers, drain a running worker request, publish, then continue.

        This does not stop the original branch. Call stop() after a successful
        checkpoint to suspend it. No partial manifest is published on failure.
        """
        async with self.lock:
            if (
                not self.active
                or self.environment is None
                or self.started is None
                or self.release is None
            ):
                raise RuntimeError("checkpoint requires an active learner trial")
            store = CheckpointStore(self.directory / "checkpoints")
            # Validate the name before stopping any writer.
            if not name or not all(c.isalnum() or c in "_-" for c in name):
                raise ValueError("checkpoint name must be a simple identifier")
            if (store.directory / (name + ".json")).exists():
                raise FileExistsError(name)
            frozen, trees, worker_state = [], {}, {}
            broker_locked = False
            try:
                await self._freeze(self.environment, frozen)
                if self.broker is not None:
                    await self.broker.gate.acquire()
                    broker_locked = True
                    if self.broker.task is not None and self.broker.task.done():
                        self.broker.task.result()
                        raise RuntimeError(
                            "cannot checkpoint a stopped resource broker"
                        )
                    for worker_name, session in self.broker.sessions.items():
                        if session.closed:
                            worker_state[worker_name] = {"closed": True}
                            continue
                        if session.environment is None:
                            raise RuntimeError(
                                "cannot checkpoint an uninitialized worker"
                            )
                        await self._freeze(session.environment, frozen)
                        trees["worker-" + worker_name] = await self._capture(
                            session.environment, session.worker.state_paths, store
                        )
                        if session.deadline <= time.monotonic():
                            raise RuntimeError(
                                "worker budget expired during checkpoint"
                            )
                        worker_state[worker_name] = {
                            "remaining_seconds": session.deadline - time.monotonic(),
                        }
                    broker_state = {
                        "seen": sorted(self.broker.seen),
                        "completed": self.broker.completed,
                        "reservations": self.broker.reservations,
                    }
                else:
                    broker_state = {"seen": [], "completed": {}}
                trees["learner"] = await self._capture(
                    self.environment, self.experiment.env.state_paths, store
                )
                with tempfile.TemporaryDirectory(
                    prefix="sprint-local-state-"
                ) as temporary:
                    archive = Path(temporary) / "release.tar"
                    with tarfile.open(archive, "w") as stream:
                        stream.add(self.release, arcname="release")
                        if self.broker is not None:
                            header = tarfile.TarInfo("broker")
                            header.type, header.mode = tarfile.DIRTYPE, 0o700
                            stream.addfile(header)
                            for item in self.broker.directory.iterdir():
                                if item.is_file():
                                    stream.add(item, arcname="broker/" + item.name)
                                elif item.is_dir():
                                    header = tarfile.TarInfo("broker/" + item.name)
                                    header.type, header.mode = tarfile.DIRTYPE, 0o700
                                    stream.addfile(header)
                                    for record in item.iterdir():
                                        if record.is_file():
                                            stream.add(
                                                record,
                                                arcname="broker/"
                                                + item.name
                                                + "/"
                                                + record.name,
                                            )
                    trees["release"] = store.import_archive(archive)
                remaining = self.seconds - (time.monotonic() - self.started)
                if not self.active or remaining <= 0:
                    raise RuntimeError(
                        "trial ended or exhausted its budget during checkpoint"
                    )
                sessions = [
                    name
                    for name, entry in trees["learner"].items()
                    if name.startswith(
                        ("logs/agent/sessions/", "installed-agent/codex-home/sessions/")
                    )
                    and name.endswith(".jsonl")
                    and entry["kind"] == "file"
                    and entry["size"]
                ]
                native = max(
                    sessions,
                    key=lambda name: (
                        trees["learner"][name].get("mtime", 0),
                        trees["learner"][name]["size"],
                    ),
                    default=None,
                )
                if (
                    self.experiment.agent.harness
                    in {"codex", "bounded-codex", "claude-code"}
                    and native is None
                ):
                    raise RuntimeError(
                        "agent has not written a resumable native session yet"
                    )
                harness_state = {}
                if self.experiment.agent.harness == "bounded-codex":
                    status_path = self.experiment.agent.options.get(
                        "live_status", "/run/sprint/budget.json"
                    )
                    status = json.loads(
                        await checked(
                            self.environment,
                            "cat -- " + shlex.quote(status_path),
                            timeout=30,
                        )
                    )
                    maximum = self.experiment.agent.options.get("max_turns", 200)
                    if status.get("maximum_actions") != maximum:
                        raise ValueError(
                            "checkpoint action limit differs from definition"
                        )
                    harness_state["actions_used"] = status.get("actions_used")
                    validate_resume_limits(
                        self.experiment,
                        {
                            "remaining_seconds": remaining,
                            "actions_used": harness_state["actions_used"],
                            "workers": worker_state,
                            "broker": broker_state,
                        },
                    )
                checkpoint = store.publish(
                    name,
                    {
                        "created_at": datetime.now(UTC).isoformat(),
                        "kind": "logical-state",
                        "definition": definition(self.experiment),
                        "backend": self.backend,
                        "parent": str(self.parent.path) if self.parent else None,
                        "remaining_seconds": remaining,
                        "workers": worker_state,
                        "broker": broker_state,
                        "trees": trees,
                        "native_session": native,
                        **harness_state,
                        "recovery": "processes and API connections restart; interrupted commands require agent inspection",
                    },
                )
                self.parent = checkpoint
                return checkpoint
            finally:
                # Always resume the original branch, even when transfer or storage fails.
                failures = []
                for environment, helper, pids in reversed(frozen):
                    try:
                        await checked(
                            environment,
                            f"python -I -S {shlex.quote(helper)} unfreeze {shlex.quote(json.dumps(pids))} && rm -f -- {shlex.quote(helper)}",
                            timeout=30,
                        )
                    except Exception as error:
                        failures.append(error)
                if broker_locked and self.broker is not None:
                    self.broker.gate.release()
                if failures:
                    raise ExceptionGroup(
                        "checkpoint unfreeze failed; stop the original branch", failures
                    )

    async def _freeze(self, environment, frozen):
        helper = await remote_helper(environment)
        pids = json.loads(
            await checked(
                environment, f"python -I -S {shlex.quote(helper)} freeze", timeout=30
            )
        )
        frozen.append((environment, helper, pids))

    async def _capture(self, environment, roots, store):
        helper = await remote_helper(environment)
        remote = "/.sprint-state-" + uuid4().hex + ".tar"
        try:
            await checked(
                environment,
                f"python -I -S {shlex.quote(helper)} pack {shlex.quote(json.dumps(roots))} {shlex.quote(remote)}",
            )
            with tempfile.TemporaryDirectory(prefix="sprint-capture-") as temporary:
                archive = Path(temporary) / "state.tar"
                await environment.download_file(remote, archive)
                return store.import_archive(archive)
        finally:
            await checked(
                environment, "rm -f -- " + shlex.join([remote, helper]), timeout=30
            )
