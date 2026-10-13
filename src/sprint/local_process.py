"""POSIX shell execution on an operator-allocated host, with account separation."""

import asyncio
import os
import pwd
import signal

from sprint.environments.base import ExecResult

SHELL_VARIABLES = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TZ")


def learner_environment(overrides=None):
    """Inherit shell settings only; credentials require explicit configuration."""
    environment = {
        name: os.environ[name] for name in SHELL_VARIABLES if name in os.environ
    }
    environment.setdefault("PATH", os.defpath)
    environment.update(overrides or {})
    return environment


async def run_shell(command, *, cwd, user, env=None, timeout_sec=None):
    """Run bash as one account; timeout/cancellation kills its process group.

    Root operator commands retain the operator environment. Non-root commands
    inherit only shell settings, then explicitly supplied task/harness variables.
    HOME, USER and LOGNAME always match the selected account.
    """
    account = pwd.getpwuid(user) if isinstance(user, int) else pwd.getpwnam(user)
    environment = os.environ.copy() if account.pw_uid == 0 else learner_environment()
    environment.update(env or {})
    environment.update(
        HOME=account.pw_dir, USER=account.pw_name, LOGNAME=account.pw_name
    )
    process = await asyncio.create_subprocess_exec(
        "/bin/bash",
        "-o",
        "pipefail",
        "-c",
        command,
        cwd=cwd,
        env=environment,
        user=account.pw_uid,
        group=account.pw_gid,
        extra_groups=[],
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout_sec)
    except BaseException:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.communicate()
        raise
    return_code = process.returncode
    if return_code is None:
        raise RuntimeError("shell completed without an exit status")
    return ExecResult(
        stdout=stdout.decode(errors="replace"),
        stderr=stderr.decode(errors="replace"),
        return_code=return_code,
    )
