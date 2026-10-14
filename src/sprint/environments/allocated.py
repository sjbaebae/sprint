"""Use an explicitly allocated Linux host account without nested containers.

The operator must opt in with /etc/sprint/allocated.json. This backend is for a
single dedicated learner account, not mutually untrusted users sharing a host.
It neither provisions the machine nor supplies network or hardware isolation.
"""

import asyncio
import json
import os
import pwd
import shutil
import socket
import stat
import tomllib
from pathlib import Path
from uuid import uuid4
from typing import override

from sprint.agents.codex_supervisor import live_processes, stop_trial_processes
from sprint.file_transfer import copy_directory, copy_file, directory_fd
from sprint.local_process import run_shell
from sprint.environments.base import BaseEnvironment
from sprint.environments.capabilities import EnvironmentCapabilities

MARKER = Path("/etc/sprint/allocated.json")


def allocated_config():
    """Read explicit host consent through non-symlinked, operator-owned paths."""
    if os.geteuid() != 0 or not Path("/proc/self/status").is_file():
        raise RuntimeError("allocated backend requires root on a dedicated Linux host")
    parent = directory_fd(MARKER.parent)
    parent_info = os.fstat(parent)
    if parent_info.st_uid != 0 or parent_info.st_mode & 0o022:
        os.close(parent)
        raise ValueError("allocated marker directory must be operator-owned")
    try:
        fd = os.open(
            MARKER.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
        )
        with os.fdopen(fd) as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != 0
                or info.st_mode & 0o077
            ):
                raise ValueError("allocated host marker must be root-owned and private")
            value = json.load(stream)
    finally:
        os.close(parent)
    account = pwd.getpwnam("agent")
    if account.pw_uid == 0 or account.pw_gid == 0 or account.pw_dir != "/home/agent":
        raise ValueError(
            "allocated learner must use a non-root account with /home/agent as its home"
        )
    if value["hostname"] != socket.gethostname() or value["uid"] != account.pw_uid:
        raise ValueError("allocated host/account identity mismatch")
    workspace, state = Path(value["workspace"]), Path(value["state"])
    if (
        not workspace.is_absolute()
        or ".." in workspace.parts
        or not workspace.is_relative_to("/app")
        or workspace == Path("/app")
        or not state.is_absolute()
        or ".." in state.parts
        or state.is_relative_to("/app")
        or state.is_relative_to(account.pw_dir)
    ):
        raise ValueError(
            "allocated workspace must be below /app; state must stay private"
        )
    for ancestor in (state, *state.parents):
        info = ancestor.stat(follow_symlinks=False)
        if info.st_uid != 0 or (
            info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX
        ):
            raise ValueError("allocated state ancestors must stay operator-owned")
    state_fd = directory_fd(state)
    try:
        info = os.fstat(state_fd)
        if info.st_uid != 0 or info.st_mode & 0o077:
            raise ValueError("allocated state directory must be root-owned and private")
    finally:
        os.close(state_fd)
    return account, workspace, state


class AllocatedEnvironment(BaseEnvironment):
    """Lease one prepared learner account; retain prior files instead of deleting them."""

    def __init__(self, *args, **kwargs):
        self.owned = False
        self.reap = False
        self.account, self.workspace, self.state = allocated_config()
        self.lease = self.state / "lease"
        kwargs.pop("enable_environment_dir_upload", None)
        super().__init__(*args, enable_environment_dir_upload=False, **kwargs)

    @staticmethod
    @override
    def type():
        return "allocated"

    @property
    @override
    def capabilities(self):
        return EnvironmentCapabilities()

    @override
    def _validate_definition(self):
        if not (self.environment_dir / "workspace").is_dir():
            raise ValueError("allocated backend requires a compiled Sprint environment")
        contract = tomllib.loads(
            (self.environment_dir.parent / "task.toml").read_text()
        )
        self.submission = Path(contract["artifacts"][0])
        if not self.submission.is_relative_to(self.workspace):
            raise ValueError(
                "allocated submission must stay inside the learner workspace"
            )

    @override
    async def start(self, force_build=False):
        """Acquire the exclusive account lease before changing any learner paths."""
        self.lease.mkdir(mode=0o700)
        self.owned = True
        try:
            if live_processes(self.account.pw_uid):
                raise RuntimeError(
                    "allocated learner account already has running processes"
                )
            self.reap = True
            archive = self.state / ("previous-" + uuid4().hex)
            archive.mkdir(mode=0o700)
            (self.lease / "owner.json").write_text(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "session": self.session_id,
                        "archive": str(archive),
                    }
                )
            )
            paths = (
                self.workspace,
                Path(self.account.pw_dir),
                Path("/installed-agent"),
                Path("/resources"),
                Path("/logs/agent"),
                Path("/solution"),
                Path("/tmp/codex-secrets"),
                Path("/run/sprint"),
            )
            for index, path in enumerate(paths):
                if path.is_symlink():
                    raise ValueError("allocated runtime path cannot be a symlink")
                if path.exists():
                    shutil.move(str(path), archive / str(index))
            self.workspace.mkdir(parents=True)
            home = Path(self.account.pw_dir)
            home.mkdir(mode=0o700)
            copy_directory(self.environment_dir / "workspace", self.workspace)
            self.submission.mkdir(parents=True, exist_ok=True)
            for path in (self.workspace, home, *self.workspace.rglob("*")):
                os.chown(path, self.account.pw_uid, self.account.pw_gid)
            resources = self.environment_dir / "resources"
            if resources.is_dir():
                copy_directory(resources, "/resources")
                for path in (Path("/resources"), *Path("/resources").rglob("*")):
                    path.chmod(0o555 if path.is_dir() else 0o444)
            if (self.environment_dir / "resource_client.py").is_file():
                copy_file(
                    self.environment_dir / "resource_client.py",
                    "/opt/sprint-resource.py",
                )
                copy_file(
                    self.environment_dir / "resource_manifest.json",
                    "/resources/control/manifest.json",
                )
                for name in ("requests", "results"):
                    path = Path("/resources/control") / name
                    path.mkdir()
                    path.chmod(0o755)
                    if name == "requests":
                        os.chown(path, self.account.pw_uid, self.account.pw_gid)
                wrapper = Path("/usr/local/bin/sprint-resource")
                wrapper.write_text(
                    '#!/bin/sh\nexec python -I -S /opt/sprint-resource.py "$@"\n'
                )
                wrapper.chmod(0o755)
            policy = self.environment_dir / "codex-requirements.toml"
            if policy.is_file():
                copy_file(policy, "/etc/codex/requirements.toml")
                Path("/etc/codex/requirements.toml").chmod(0o444)
            logs = Path("/logs/agent")
            logs.mkdir(parents=True, exist_ok=True)
            logs.chmod(0o1777)
        except BaseException:
            await self.stop()
            raise

    @override
    async def stop(self, delete=False):
        """Reap only the leased learner account; preserve files for operator inspection."""
        if self.owned:
            if self.reap:
                await asyncio.to_thread(stop_trial_processes, self.account.pw_uid)
            shutil.rmtree(self.lease)
            self.owned = False

    @override
    async def exec(self, command, cwd=None, env=None, timeout_sec=None, user=None):
        return await run_shell(
            command,
            cwd=cwd or str(self.workspace),
            user=self._resolve_user(user) or "root",
            env=self._merge_env(env),
            timeout_sec=timeout_sec,
        )

    @override
    async def upload_file(self, source_path, target_path):
        copy_file(source_path, target_path)

    @override
    async def upload_dir(self, source_dir, target_dir):
        copy_directory(source_dir, target_dir)

    @override
    async def download_file(self, source_path, target_path):
        copy_file(source_path, target_path)

    @override
    async def download_dir(self, source_dir, target_dir):
        shutil.copytree(source_dir, target_dir, dirs_exist_ok=True, symlinks=True)
