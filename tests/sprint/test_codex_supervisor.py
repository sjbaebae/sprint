"""Supervisor records are safe against learner-created filesystem entries."""

import json
import os

import pytest

from sprint.agents.codex_supervisor import TurnCounter, child_environment, write_status


def test_supervisor_passes_harness_auth_but_not_provider_credentials(monkeypatch):
    monkeypatch.setenv("MODAL_TOKEN_SECRET", "synthetic operator secret")
    monkeypatch.setenv("RUNPOD_API_KEY", "synthetic operator key")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic harness key")
    environment = child_environment()
    assert "MODAL_TOKEN_SECRET" not in environment
    assert "RUNPOD_API_KEY" not in environment
    assert environment["OPENAI_API_KEY"] == "synthetic harness key"


def test_actions_count_once_and_reasoning_does_not_count():
    counter = TurnCounter()
    for event in [
        {"type": "item.started", "item": {"id": "a", "type": "command_execution"}},
        {"type": "item.completed", "item": {"id": "a", "type": "command_execution"}},
        {"type": "item.completed", "item": {"id": "b", "type": "reasoning"}},
        {"type": "item.completed", "item": {"id": "c", "type": "agent_message"}},
    ]:
        counter.observe(json.dumps(event))
    assert counter.observe("invalid json") == 2


@pytest.mark.skipif(os.name != "posix", reason="directory descriptors require POSIX")
def test_status_replaces_symlink_without_touching_target(tmp_path):
    target = tmp_path / "private"
    target.write_text("unchanged")
    (tmp_path / "budget.json").symlink_to(target)
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        write_status(descriptor, "budget.json", {"state": "finished"})
    finally:
        os.close(descriptor)
    assert target.read_text() == "unchanged"
    assert not (tmp_path / "budget.json").is_symlink()
    assert json.loads((tmp_path / "budget.json").read_text()) == {"state": "finished"}


def test_bounded_harness_forces_single_agent_and_managed_profile(tmp_path):
    from sprint.agents.installed.bounded_codex import BoundedCodex

    agent = BoundedCodex(
        logs_dir=tmp_path,
        permission_profile="research",
        config={"features": {"multi_agent": True}, "web_search": "live"},
    )
    config = agent._build_effective_config()
    assert config["default_permissions"] == "research"
    assert config["features"]["multi_agent"] is False
    assert config["features"]["multi_agent_v2"] is False
    assert config["web_search"] == "live"


@pytest.mark.asyncio
async def test_learner_receives_enforced_budget(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    from sprint.agents.installed.codex import Codex
    from sprint.agents.installed.bounded_codex import BoundedCodex

    run = AsyncMock()
    monkeypatch.setattr(Codex, "run", run)
    agent = BoundedCodex(logs_dir=tmp_path, seconds=120, max_turns=10)
    await agent.run("Research task", object(), object())
    prompt = run.call_args.args[0]
    assert prompt.startswith("Research task")
    assert "120 seconds and 10 remaining actions" in prompt
    assert agent.live_status in prompt


def test_restored_counter_preserves_spent_actions_without_reusing_item_ids():
    counter = TurnCounter(actions_used=7)
    event = json.dumps(
        {
            "type": "item.completed",
            "item": {"id": "item_0", "type": "command_execution"},
        }
    )
    assert counter.observe(event) == 8
    assert counter.observe(event) == 8
    assert counter.used == 8


def test_bounded_resume_requires_trusted_action_count(tmp_path):
    from sprint.agents.installed.bounded_codex import BoundedCodex

    native = tmp_path / "rollout-2026-10-13T00-00-00-session.jsonl"
    native.write_text('{"type":"session_meta"}\n')
    with pytest.raises(ValueError, match="restored actions_used"):
        BoundedCodex(logs_dir=tmp_path, load_trajectory=native)
    agent = BoundedCodex(logs_dir=tmp_path, load_trajectory=native, actions_used=7)
    assert agent.actions_used == 7


@pytest.mark.asyncio
async def test_allocated_codex_requires_pinned_preinstalled_cli(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from sprint.agents.installed.bounded_codex import BoundedCodex
    from sprint.agents.installed.codex import Codex

    installer = AsyncMock()
    monkeypatch.setattr(Codex, "install", installer)
    environment = SimpleNamespace(
        type=lambda: "allocated",
        exec=AsyncMock(
            return_value=SimpleNamespace(return_code=0, stdout="codex-cli 0.161.0")
        ),
    )
    agent = BoundedCodex(logs_dir=tmp_path, version="0.162.1")
    with pytest.raises(RuntimeError, match="preinstalled Codex version"):
        await agent.install(environment)
    environment.exec.return_value.stdout = "codex-cli 0.162.1"
    await agent.install(environment)
    installer.assert_not_awaited()
