"""External-host learner commands must not inherit operator credentials."""

import asyncio
from types import SimpleNamespace

import pytest

import sprint.local_process as runtime


@pytest.mark.asyncio
async def test_learner_shell_filters_environment_and_kills_timed_out_group(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("OPERATOR_CREDENTIAL_CANARY", "private-value")
    monkeypatch.setattr(
        runtime.pwd,
        "getpwuid",
        lambda uid: SimpleNamespace(
            pw_uid=1000,
            pw_gid=1000,
            pw_name="agent",
            pw_dir=str(tmp_path),
        ),
    )
    spawn = asyncio.create_subprocess_exec
    processes = []

    async def local_spawn(*args, **kwargs):
        assert kwargs["user"] == 1000
        for name in ("user", "group", "extra_groups"):
            kwargs.pop(name)
        process = await spawn(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", local_spawn)
    result = await runtime.run_shell(
        'printf "%s|%s" "$OPERATOR_CREDENTIAL_CANARY" "$EXPLICIT_SETTING"',
        cwd=tmp_path,
        user=1000,
        env={"EXPLICIT_SETTING": "enabled"},
        timeout_sec=5,
    )
    assert result.return_code == 0 and result.stdout == "|enabled"
    with pytest.raises(TimeoutError):
        await runtime.run_shell("sleep 30", cwd=tmp_path, user=1000, timeout_sec=0.05)
    assert processes[-1].returncode == -9
