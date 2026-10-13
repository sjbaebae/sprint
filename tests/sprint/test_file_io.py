"""Bounded reads must not escape through learner-controlled links."""

import pytest

from sprint.file_io import read_regular_json


def test_workspace_json_is_bounded_and_rejects_symlink_parents(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    source = workspace / "request.json"
    source.write_text('{"kind":"submit"}')
    assert read_regular_json(source, 100, workspace) == {"kind": "submit"}
    with pytest.raises(ValueError, match="too large"):
        read_regular_json(source, 2, workspace)
    outside = tmp_path / "private"
    outside.mkdir()
    (outside / "secret.json").write_text("{}")
    (workspace / "alias").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        read_regular_json(workspace / "alias/secret.json", 100, workspace)
