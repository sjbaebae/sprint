"""Unit tests for simple agents that have install() methods."""

import os
from unittest.mock import AsyncMock, patch

import pytest

from sprint.agents.installed.aider import Aider
from sprint.agents.installed.claude_code import ClaudeCode
from sprint.agents.installed.codex import Codex
from sprint.agents.installed.cortex_code import CortexCode
from sprint.agents.installed.cursor_cli import CursorCli
from sprint.agents.installed.fx import Fx
from sprint.agents.installed.fx_dev import FxDev
from sprint.agents.installed.gemini_cli import GeminiCli
from sprint.agents.installed.rovodev_cli import RovodevCli
from sprint.agents.installed.goose import Goose
from sprint.agents.installed.grok_build import GrokBuild
from sprint.agents.installed.hermes import Hermes
from sprint.agents.installed.junie import Junie
from sprint.agents.installed.kimi_code import KimiCode
from sprint.agents.installed.kimi_cli import KimiCli
from sprint.agents.installed.mini_swe_agent import MiniSweAgent
from sprint.agents.installed.mcode import MCode
from sprint.agents.installed.opencode import OpenCode
from sprint.agents.installed.pi import Pi
from sprint.agents.installed.qwen_code import QwenCode
from sprint.agents.installed.swe_agent import SweAgent
from sprint.agents.installed.trae_agent import TraeAgent


class TestSimpleAgentInstall:
    """Test agents have a callable install() method."""

    @pytest.mark.parametrize(
        "agent_class",
        [
            Aider,
            ClaudeCode,
            Codex,
            CortexCode,
            CursorCli,
            Fx,
            FxDev,
            GeminiCli,
            RovodevCli,
            Goose,
            GrokBuild,
            Hermes,
            Junie,
            KimiCode,
            KimiCli,
            MiniSweAgent,
            MCode,
            OpenCode,
            Pi,
            QwenCode,
            SweAgent,
            TraeAgent,
        ],
    )
    def test_agent_has_install_method(self, agent_class, temp_dir):
        """Test that agents have an install() method."""
        with patch.dict(os.environ, clear=False):
            agent = agent_class(logs_dir=temp_dir)
            assert hasattr(agent, "install")
            assert callable(agent.install)

    @pytest.mark.parametrize(
        "agent_class",
        [
            Aider,
            ClaudeCode,
            Codex,
            CortexCode,
            CursorCli,
            Fx,
            FxDev,
            GeminiCli,
            RovodevCli,
            Goose,
            GrokBuild,
            Hermes,
            Junie,
            KimiCode,
            KimiCli,
            MiniSweAgent,
            MCode,
            OpenCode,
            Pi,
            QwenCode,
            SweAgent,
            TraeAgent,
        ],
    )
    @pytest.mark.asyncio
    async def test_agent_install_calls_exec(self, agent_class, temp_dir):
        """Test that install() calls environment.exec()."""
        with patch.dict(os.environ, clear=False):
            agent = agent_class(logs_dir=temp_dir)
            environment = AsyncMock()
            environment.exec.return_value = AsyncMock(
                return_code=0, stdout="", stderr=""
            )
            environment.upload_file.return_value = None
            try:
                await agent.install(environment)
            except Exception:
                pass  # Some agents may fail due to missing resources, that's OK
            # Verify exec was called at least once
            assert environment.exec.call_count >= 1
