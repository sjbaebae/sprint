"""Checkpoint export must never follow learner-controlled paths outside /app."""

import hashlib
import os
from pathlib import Path

import pytest

from sprint import worker_export


@pytest.mark.parametrize("volume", [False, True])
def test_export_hashes_regular_file(tmp_path, monkeypatch, volume):
    monkeypatch.setattr(worker_export, "ROOT", str(tmp_path))
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    monkeypatch.setattr(worker_export, "ARTIFACT_ROOT", str(private))
    (tmp_path / "checkpoint").write_bytes(b"checkpoint")
    result = worker_export.snapshot("checkpoint", volume=volume)
    exported = Path(result["path"])
    try:
        assert exported.read_bytes() == b"checkpoint"
        assert result["sha256"] == hashlib.sha256(b"checkpoint").hexdigest()
        assert result["bytes"] == 10
        if volume:
            assert exported.parent == private
            assert exported.stat().st_mode & 0o777 == 0o600
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


def test_operator_limit_can_allow_larger_checkpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(worker_export, "ROOT", str(tmp_path))
    monkeypatch.setattr(worker_export, "LIMIT", 2)
    (tmp_path / "checkpoint").write_bytes(b"larger")
    with pytest.raises(ValueError, match="at most"):
        worker_export.snapshot("checkpoint")
    result = worker_export.snapshot("checkpoint", max_bytes=6)
    try:
        assert result["bytes"] == 6
        assert Path(result["path"]).read_bytes() == b"larger"
    finally:
        Path(result["path"]).unlink()
