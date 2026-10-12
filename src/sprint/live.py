"""Checkpoint/resume logical RL runs without saving process or GPU memory."""

import asyncio
import json
import math
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


def prepare_restore(experiment, source, directory):
    """Validate state and materialize the exact compiled release before launch."""
    checkpoint = source if isinstance(source, Checkpoint) else Checkpoint(source)
    checkpoint.verify()
    metadata = checkpoint.metadata
    if metadata["definition"] != definition(experiment):
        raise ValueError("checkpoint belongs to a different experiment definition")
    remaining = metadata["remaining_seconds"]
    if (
        not isinstance(remaining, (int, float))
        or not math.isfinite(remaining)
        or remaining <= 0
    ):
        raise ValueError("checkpoint has no remaining trial budget")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
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
            "claude-code",
            "oracle",
            "nop",
        }:
            raise ValueError(
                "checkpointed runs currently support Codex, Claude Code, oracle and nop"
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
                    self.experiment.agent.harness in {"codex", "claude-code"}
                    and native is None
                ):
                    raise RuntimeError(
                        "agent has not written a resumable native session yet"
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
