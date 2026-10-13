"""Operator transfers must not follow learner-controlled symlinks."""

import pytest

from sprint.file_transfer import copy_file


def test_transfer_replaces_target_link_and_rejects_source_link(tmp_path):
    root = tmp_path.resolve()
    source = root / "source"
    source.write_text("learner output")
    secret = root / "secret"
    secret.write_text("protected")
    target = root / "target"
    target.symlink_to(secret)
    copy_file(source, target)
    assert not target.is_symlink()
    assert target.read_text() == "learner output"
    assert secret.read_text() == "protected"
    alias = root / "alias"
    alias.symlink_to(source)
    with pytest.raises(OSError):
        copy_file(alias, target)
