"""Operator-owned Modal resource for an external learner host.

The learner receives SSH access to its account. Sprint owns provisioning, lifetime,
observations, immutable artifact collection and teardown; it never runs training.
"""

import asyncio
import json
import shlex
import time
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import modal

from sprint.learner_ssh import create_identity, publish_connection
from sprint.workers import WorkerSession
from sprint.workspace_observations import collector_source


def cleanup_resources(state):
    """Recover recorded sandboxes and artifact volumes after a host interruption.

    State must be operator-owned. Each successful cleanup is recorded separately;
    failure of one provider operation does not prevent attempts on other resources.
    Function-call cancellation remains the responsibility of the invoking service.
    """
    state = Path(state)
    errors = []
    for pattern, identity, done in (
        ("*.sandbox.json", "sandbox_id", "terminated"),
        ("*.artifact-volume.json", "volume_name", "deleted"),
    ):
        for path in sorted(state.glob(pattern)):
            try:
                record = json.loads(path.read_text())
                if record.get(done) or not record.get(identity):
                    continue
                if identity == "sandbox_id":
                    modal.Sandbox.from_id(record[identity]).terminate(wait=True)
                else:
                    modal.Volume.objects.delete(record[identity], allow_missing=True)
                record[done] = True
                path.write_text(json.dumps(record))
            except Exception as error:
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
        self.volume = None
        self.identity = self.state / "learner-ssh"
        self.record = self.state / "persistent.sandbox.json"
        self.volume_record = self.state / "persistent.artifact-volume.json"

    def start(self):
        """Provision lazily, with the remaining trial budget as provider lifetime."""
        if self.closed:
            raise RuntimeError("worker is closed")
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
            self.volume = "sprint-artifacts-" + uuid4().hex
            # Record ownership before the provider call, including failed creation.
            self.volume_record.write_text(
                json.dumps({"volume_name": self.volume, "deleted": False})
            )
            modal.Volume.from_name(
                self.volume, create_if_missing=True, version=2
            ).hydrate()
            session = WorkerSession(
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
            self.session = session

            async def provision():
                await session.__aenter__()
                await session.start()

            self.runner.run(provision())
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
                        "provisioning_started_at": started,
                        "provisioning_seconds": time.time() - started,
                        "started_at": time.time(),
                        "terminated": False,
                    }
                )
            )
            return session
        except BaseException as error:
            try:
                self.close()
            finally:
                self.record.write_text(
                    json.dumps(
                        {
                            "trial": self.trial,
                            "status": "provisioning_failed",
                            "provisioning_started_at": started,
                            "provisioning_seconds": time.time() - started,
                            "error": str(error),
                            "terminated": self.session is None,
                        }
                    )
                )
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
