"""Native Sprint Codex adapter with a root-owned, bounded execution supervisor."""

from sprint.agents import codex_supervisor
from pydantic import Field
from pathlib import Path
import shlex
from typing import override
from sprint.agents.installed.codex import Codex, CodexOptions


class BoundedCodexOptions(CodexOptions):
    permission_profile: str = "offline"
    live_status: str = "/run/sprint/budget.json"
    max_turns: int = Field(default=200, ge=1, le=200)
    seconds: float = Field(default=7200, gt=0, le=7200)


class BoundedCodex(Codex):
    options_model = BoundedCodexOptions

    @override
    async def setup(self, environment):
        """Install Codex and protect supervisor budget records."""
        await super().setup(environment)
        protected = await super().exec_as_root(
            environment,
            command="chown root:root /logs /logs/agent && chmod 1777 /logs /logs/agent",
        )
        if protected.return_code:
            raise RuntimeError("Budget log directory protection failed")

    def __init__(
        self,
        *args,
        max_turns=200,
        seconds=7200,
        permission_profile="offline",
        live_status="/run/sprint/budget.json",
        **kwargs,
    ):
        if type(max_turns) is not int or not 1 <= max_turns <= 200:
            raise ValueError("invalid Codex action budget")
        if type(seconds) not in (int, float) or not 0 < seconds <= 7200:
            raise ValueError("invalid Codex time budget")
        if not permission_profile or not all(
            c.isalnum() or c in "_-" for c in permission_profile
        ):
            raise ValueError("invalid managed permission profile")
        self.max_turns = max_turns
        self.seconds = seconds
        self.permission_profile = permission_profile
        self.live_status = live_status
        super().__init__(*args, **kwargs)

    async def before_supervised_exec(self, environment):
        """Optional task adapter hook; Sprint does not start task services."""

    @override
    def _build_effective_config(self, openai_base_url=None):
        """Apply the managed profile and disable subagents for bounded trials."""
        config = super()._build_effective_config(openai_base_url)
        config.pop("sandbox_mode", None)
        config.pop("sandbox_workspace_write", None)
        config["default_permissions"] = self.permission_profile
        features = config.setdefault("features", {})
        features.update(multi_agent=False, multi_agent_v2=False)
        # The profile is defined by /etc/codex/requirements.toml. Codex rejects
        # redefining a managed profile in user config, even with identical values.
        config.pop("permissions", None)
        # Effort does not enable readable summaries. Record summaries explicitly
        # so native session exports can support subsequent research review.
        config["model_reasoning_summary"] = "detailed"
        return config

    @override
    async def run(self, instruction, environment, context):
        """Tell the learner the exact limits enforced by its supervisor."""
        limits = (
            f"\n\nExecution budget (enforced): {self.seconds:g} seconds and "
            f"{self.max_turns} actions. These limits override task defaults. "
            "Tool calls and agent messages count as actions; reasoning summaries do not. "
            f"Read {self.live_status} for current budget status. No subagents."
        )
        await super().run(instruction + limits, environment, context)

    @override
    async def exec_as_agent(
        self, environment, command, env=None, cwd=None, timeout_sec=None
    ):
        """Wrap native Codex execution in a root supervisor with UID/time/turn limits.

        Non-Codex setup commands use the normal Sprint path. Returns ExecResult;
        rejects failed authentication/protection/startup. Task services are supplied by the optional adapter hook.
        """
        kwargs = {"env": env, "cwd": cwd, "timeout_sec": timeout_sec}
        if "codex exec " not in command:
            return await super().exec_as_agent(environment, command=command, **kwargs)
        command = command.replace(
            "--dangerously-bypass-approvals-and-sandbox",
            f"-c default_permissions={self.permission_profile} -c approval_policy=never",
        )
        supervisor = "/opt/sprint-codex-supervisor.py"
        await environment.upload_file(Path(codex_supervisor.__file__), supervisor)
        await super().exec_as_root(
            environment,
            command=f"chown root:root {supervisor} && chmod 444 {supervisor}",
        )
        identity = await super().exec_as_agent(environment, command="id -u; id -g")
        uid, gid = identity.stdout.strip().splitlines()[-2:]
        # Sprint uploads config files as root; restrictive host permissions must
        # not prevent the unprivileged learner from reading its own CLI config.
        await super().exec_as_root(
            environment,
            command=f"chown -R {int(uid)}:{int(gid)} /installed-agent/codex-home /tmp/codex-secrets && "
            "chmod -R u+rwX /installed-agent/codex-home /tmp/codex-secrets",
        )
        await super().exec_as_agent(
            environment, command="codex login status", env=kwargs.get("env")
        )
        await self.before_supervised_exec(environment)
        wrapped = (
            "python "
            + supervisor
            + f" --max-turns {self.max_turns} --seconds {self.seconds} "
            f"--live-status {shlex.quote(self.live_status)} "
            f"--uid {int(uid)} --gid {int(gid)} "
            f"--status {shlex.quote(str(self.environment_logs_dir / 'budget.json'))} "
            "--command " + shlex.quote(command)
        )
        return await super().exec_as_root(environment, command=wrapped, **kwargs)
