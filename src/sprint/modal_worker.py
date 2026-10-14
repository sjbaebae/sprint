"""Operator-owned Modal resource for an external learner host.

The learner receives SSH access to its account. Sprint owns provisioning, lifetime,
observations, immutable artifact collection and teardown; it never runs training.
"""

import asyncio
import hashlib
import json
import math
import shlex
import time
from dataclasses import asdict, replace
from pathlib import Path
from uuid import uuid4

import modal

from sprint.checkpoint_store import Checkpoint, CheckpointStore
from sprint.learner_ssh import create_identity, publish_connection
from sprint.state_transfer import capture_tree, checked, remote_helper, restore_tree
from sprint.workers import MODAL_WORKER_APP, WorkerSession
from sprint.workspace_observations import collector_source


def state_runtime_hashes():
    """Pin transfer and execution helpers rather than silently changing recovery."""
    root = Path(__file__).parent
    return {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        for name in (
            "remote_state.py",
            "state_transfer.py",
            "checkpoint_store.py",
            "worker_run.py",
            "worker_export.py",
        )
    }


def _cancel_call_tree(call_id, *, terminate_containers):
    """Stop each creator before discovering any calls it already dispatched."""
    pending = [call_id]
    seen = set()
    while pending:
        identity = pending.pop()
        if identity in seen:
            continue
        seen.add(identity)
        call = modal.FunctionCall.from_id(identity)
        call.cancel(terminate_containers=terminate_containers)
        nodes = list(call.get_call_graph())
        while nodes:
            node = nodes.pop()
            nodes.extend(node.children)
            if node.function_call_id not in seen:
                pending.append(node.function_call_id)


def cleanup_resources(state):
    """Cancel owned calls, then recover their sandboxes and artifact volumes.

    State must be operator-owned. A pre-recorded session_id can identify a sandbox
    even if its creating host died before recording the provider ID. Each cleanup
    is retained independently; failures do not prevent attempts on other resources.
    Set terminate_containers only for calls whose containers are exclusively owned.
    """
    state = Path(state)
    errors = []
    creators_stopped = True
    for pattern, done in (
        ("*.call.json", "cancelled"),
        ("*.sandbox.json", "terminated"),
        ("*.artifact-volume.json", "deleted"),
    ):
        for path in sorted(state.glob(pattern)):
            try:
                record = json.loads(path.read_text())
                if record.get(done):
                    continue
                if done == "cancelled":
                    if not record.get("call_id"):
                        continue
                    _cancel_call_tree(
                        record["call_id"],
                        terminate_containers=record.get("terminate_containers", False),
                    )
                elif done == "terminated":
                    if record.get("sandbox_id"):
                        sandboxes = [modal.Sandbox.from_id(record["sandbox_id"])]
                    elif record.get("session_id"):
                        app = modal.App.lookup(record.get("app_name", MODAL_WORKER_APP))
                        sandboxes = modal.Sandbox.list(
                            app_id=app.app_id,
                            tags={
                                "sprint.managed": "true",
                                "sprint.session_id": record["session_id"],
                            },
                        )
                    else:
                        continue
                    for sandbox in sandboxes:
                        sandbox.terminate(wait=True)
                else:
                    if not record.get("volume_name"):
                        continue
                    modal.Volume.objects.delete(
                        record["volume_name"], allow_missing=True
                    )
                # A failed creator cancellation can still allocate new resources.
                # Retain recovery work until every owned creator is stopped.
                record[done] = done == "cancelled" or creators_stopped
                path.write_text(json.dumps(record))
            except Exception as error:
                if done == "cancelled":
                    creators_stopped = False
                errors.append(error)
    if errors:
        raise ExceptionGroup("Modal resource recovery failed", errors)


class ModalWorker:
    """Synchronous resource lifecycle for a host using LocalRequestQueue.

    Calls must be serialized. The host must call close on trial completion or
    cancellation. Private state also records provider IDs for emergency cleanup.
    """

    def __init__(
        self,
        worker,
        *,
        state,
        connection_directory,
        deadline,
        trial,
        uid=1000,
        gid=1000,
    ):
        if not worker.shell or worker.backend not in (None, "modal"):
            raise ValueError("ModalWorker requires a Modal shell worker")
        self.worker = worker
        self.state = Path(state)
        self.connection_directory = Path(connection_directory)
        self.deadline = deadline
        self.trial = trial
        self.uid = uid
        self.gid = gid
        self.session = None
        self.runner = asyncio.Runner()
        self.closed = False
        self.quiesce_status = None
        self.volume = None
        self.identity = self.state / "learner-ssh"
        self.record = self.state / "persistent.sandbox.json"
        self.volume_record = self.state / "persistent.artifact-volume.json"

    def start(self):
        """Provision lazily, bounded by the remaining resource lifetime."""
        if self.closed:
            raise RuntimeError("worker is closed")
        if self.quiesce_status is not None:
            raise RuntimeError(
                "worker is quiescing; only observation and cleanup remain"
            )
        if self.session is not None:
            return self.session
        seconds = int(self.deadline - time.time())
        if seconds < 30:
            raise TimeoutError("insufficient time to provision worker")
        started = time.time()
        self.record.write_text(
            json.dumps(
                {
                    "trial": self.trial,
                    "status": "provisioning",
                    "provisioning_started_at": started,
                    "terminated": False,
                }
            )
        )
        try:
            public_key = create_identity(self.identity)
            volume_name = "sprint-artifacts-" + uuid4().hex
            self.volume = volume_name
            # Record ownership before the provider call, including failed creation.
            self.volume_record.write_text(
                json.dumps({"volume_name": self.volume, "deleted": False})
            )

            async def provision():
                remaining = self.deadline - time.time()
                async with asyncio.timeout(remaining):
                    await modal.Volume.from_name(
                        volume_name, create_if_missing=True, version=2
                    ).hydrate.aio()
                    seconds = int(self.deadline - time.time())
                    if seconds < 1:
                        raise TimeoutError("worker provisioning deadline reached")
                    self.session = WorkerSession(
                        replace(
                            self.worker,
                            seconds=min(self.worker.seconds, seconds),
                            lifetime_seconds=min(self.worker.lifetime_seconds, seconds),
                        ),
                        directory=self.state / "gpu-worker",
                        backend="modal",
                        artifact_volume=self.volume,
                        learner_ssh_public_key=public_key,
                    )
                    record = json.loads(self.record.read_text())
                    record.update(
                        session_id=self.session.session_id, app_name=MODAL_WORKER_APP
                    )
                    self.record.write_text(json.dumps(record))
                    await self.session.__aenter__()
                    await self.session.start()

            self.runner.run(provision())
            session = self.session
            if session is None:
                raise RuntimeError("worker provisioning returned no session")
            environment = session.environment
            sandbox = getattr(environment, "_sandbox", None)
            if sandbox is None:
                raise RuntimeError("Modal worker started without a sandbox identity")
            self.record.write_text(
                json.dumps(
                    {
                        "trial": self.trial,
                        "status": "ready",
                        "persistent": True,
                        "sandbox_id": sandbox.object_id,
                        "session_id": session.session_id,
                        "app_name": MODAL_WORKER_APP,
                        "provisioning_started_at": started,
                        "provisioning_seconds": time.time() - started,
                        "started_at": time.time(),
                        "terminated": False,
                    }
                )
            )
            return session
        except BaseException as error:
            record = json.loads(self.record.read_text())
            environment = getattr(self.session, "environment", None)
            sandbox = getattr(environment, "_sandbox", None)
            if sandbox is not None:
                record["sandbox_id"] = sandbox.object_id
            record.update(
                status="provisioning_failed",
                provisioning_seconds=time.time() - started,
                error=str(error),
            )
            self.record.write_text(json.dumps(record))
            self.close()
            raise

    def connect(self):
        """Return a learner-readable, host-key-pinned SSH configuration path."""
        session = self.start()
        return publish_connection(
            session.learner_ssh_endpoint,
            self.identity,
            self.connection_directory,
            uid=self.uid,
            gid=self.gid,
        )

    def observe(self, workspace="/app", *, start_after=""):
        """Collect bounded files and socket samples without giving away an index."""
        if self.closed or self.session is None or self.session.environment is None:
            raise RuntimeError("worker is not running")
        command = "python -I -S -c " + shlex.quote(collector_source())
        command += " " + shlex.quote(str(workspace)) + " " + shlex.quote(start_after)
        result = self.runner.run(
            self.session.environment.exec(command, user="root", timeout_sec=15)
        )
        if result.return_code:
            raise RuntimeError("worker observation failed: " + str(result.stderr))
        return json.loads(result.stdout)

    def collect(self, relative_path):
        """Freeze a learner-selected artifact in private provider storage."""
        session = self.start()
        return self.runner.run(session.collect_to_volume(relative_path))

    def checkpoint(self, directory, name):
        """Pause this worker and save logical state; close the original afterward.

        The caller must first stop learner requests and pause the CPU agent.
        Saved files, specification and remaining lifetime use the ordinary Sprint
        checkpoint store. Processes and provider connections are not saved.
        """
        if self.closed or self.session is None or self.session.environment is None:
            raise RuntimeError("checkpoint requires a running worker")
        store = CheckpointStore(directory)
        store.ensure_new_name(name)
        self.quiesce()
        entries = self.runner.run(
            capture_tree(self.session.environment, self.worker.state_paths, store)
        )
        remaining = min(
            self.deadline - time.time(), self.session.deadline - time.monotonic()
        )
        if remaining <= 0:
            raise RuntimeError("worker lifetime expired during checkpoint")
        return store.publish(
            name,
            {
                "kind": "external-worker",
                "worker_definition": json.loads(json.dumps(asdict(self.worker))),
                "runtime_hashes": state_runtime_hashes(),
                "remaining_seconds": remaining,
                "trees": {"worker": entries},
            },
        )

    def restore(self, source):
        """Restore into a fresh worker and return refreshed learner SSH access."""
        if (
            self.closed
            or self.session is not None
            or self.record.exists()
            or self.quiesce_status is not None
        ):
            raise RuntimeError("restore requires a fresh worker state directory")
        saved = source if isinstance(source, Checkpoint) else Checkpoint(source)
        saved.verify()
        metadata = saved.metadata
        expected = json.loads(json.dumps(asdict(self.worker)))
        if (
            metadata.get("kind") != "external-worker"
            or metadata.get("worker_definition") != expected
        ):
            raise ValueError("checkpoint belongs to a different worker definition")
        if metadata.get("runtime_hashes") != state_runtime_hashes():
            raise ValueError(
                "checkpoint state helper differs; use the original Sprint build"
            )
        remaining = metadata.get("remaining_seconds")
        if (
            type(remaining) not in (int, float)
            or not math.isfinite(remaining)
            or not 0 < remaining <= self.worker.lifetime_seconds
        ):
            raise ValueError("invalid checkpoint worker lifetime")
        trees = metadata.get("trees", {})
        roots = tuple(path.lstrip("/") for path in self.worker.state_paths)
        if set(trees) != {"worker"} or any(
            not any(path == root or path.startswith(root + "/") for root in roots)
            for path in trees["worker"]
        ):
            raise ValueError("checkpoint state escapes its worker roots")
        self.deadline = min(self.deadline, time.time() + remaining)
        try:
            session = self.start()
            self.runner.run(
                restore_tree(
                    session.environment, saved, "worker", self.worker.state_paths
                )
            )
            record = json.loads(self.record.read_text())
            record["restored_from"] = str(saved.path)
            self.record.write_text(json.dumps(record))
            return self.connect()
        except BaseException:
            self.close()
            raise

    def quiesce(self):
        """Revoke learner SSH and pause its processes for final inspection.

        This is irreversible for this resource. Observe the remaining files, then
        call close(). It does not save process memory or prove data provenance.
        Calls must be serialized with all other worker operations.
        """
        if self.quiesce_status == "complete":
            return
        if self.quiesce_status is not None:
            raise RuntimeError("worker quiesce did not complete; close the resource")
        if self.closed:
            raise RuntimeError("worker is closed")
        if self.session is None:
            return
        environment = self.session.environment
        if environment is None:
            raise RuntimeError("worker has no running environment")
        self.quiesce_status = "pending"

        async def pause():
            # This file is root-owned, outside the saved learner workspace.
            await checked(
                environment,
                "rm -f -- /run/sprint-learner-ssh/authorized_keys",
                timeout=30,
            )
            helper = await remote_helper(environment)
            try:
                await checked(
                    environment,
                    f"python -I -S {shlex.quote(helper)} freeze",
                    timeout=30,
                )
            finally:
                await checked(
                    environment, "rm -f -- " + shlex.quote(helper), timeout=30
                )

        try:
            self.runner.run(pause())
            self.quiesce_status = "complete"
        except BaseException:
            self.quiesce_status = "failed"
            raise
        finally:
            record = json.loads(self.record.read_text())
            record["quiesce_status"] = self.quiesce_status
            self.record.write_text(json.dumps(record))

    def close(self):
        """Attempt both worker teardown and volume deletion, retaining failed IDs."""
        if self.closed and self.session is None and self.volume is None:
            return
        self.closed = True
        errors = []
        if self.session is not None:
            try:
                self.runner.run(self.session.close())
                self.session = None
                if self.record.exists():
                    record = json.loads(self.record.read_text())
                    record["terminated"] = True
                    self.record.write_text(json.dumps(record))
            except Exception as error:
                errors.append(error)
        if self.volume is not None:
            try:
                modal.Volume.objects.delete(self.volume, allow_missing=True)
                self.volume_record.write_text(
                    json.dumps({"volume_name": self.volume, "deleted": True})
                )
                self.volume = None
            except Exception as error:
                errors.append(error)
        if self.session is None:
            self.runner.close()
        if errors:
            raise ExceptionGroup("Modal worker cleanup failed", errors)
