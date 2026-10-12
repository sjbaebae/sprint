"""Supervisor records are safe against learner-created filesystem entries."""

import json
import os

import pytest

from sprint.agents.codex_supervisor import TurnCounter, write_status


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
