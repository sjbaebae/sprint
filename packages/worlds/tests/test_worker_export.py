"""Checkpoint export must never follow learner-controlled paths outside /app."""

import hashlib
import os
from pathlib import Path

import pytest

from worlds import worker_export


def test_export_hashes_regular_file(tmp_path, monkeypatch):
    monkeypatch.setattr(worker_export, "ROOT", str(tmp_path))
    (tmp_path / "checkpoint").write_bytes(b"checkpoint")
    result = worker_export.snapshot("checkpoint")
    exported = Path(result["path"])
    try:
        assert exported.read_bytes() == b"checkpoint"
        assert result["sha256"] == hashlib.sha256(b"checkpoint").hexdigest()
        assert result["bytes"] == 10
    finally:
        exported.unlink()


@pytest.mark.parametrize("name", ["../secret", "/etc/passwd", "."])
def test_export_rejects_paths(tmp_path, monkeypatch, name):
    monkeypatch.setattr(worker_export, "ROOT", str(tmp_path))
    with pytest.raises(ValueError):
        worker_export.snapshot(name)


def test_export_rejects_symlink_parents_fifo_and_oversize(tmp_path, monkeypatch):
    monkeypatch.setattr(worker_export, "ROOT", str(tmp_path))
    (tmp_path / "link").symlink_to("/etc", target_is_directory=True)
    os.mkfifo(tmp_path / "fifo")
    with pytest.raises(OSError):
        worker_export.snapshot("link/passwd")
    with pytest.raises(ValueError, match="regular file"):
        worker_export.snapshot("fifo")
    (tmp_path / "large").write_bytes(b"large")
    monkeypatch.setattr(worker_export, "LIMIT", 2)
    with pytest.raises(ValueError, match="regular file"):
        worker_export.snapshot("large")
