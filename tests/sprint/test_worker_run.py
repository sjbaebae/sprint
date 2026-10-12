"""Trusted worker launcher rejects unsafe inputs and bounds output feedback."""

import io

import pytest

from sprint import worker_run


@pytest.mark.parametrize(
    "source",
    [
        "/etc/passwd",
        "/app/../etc/passwd",
        "/app/job.py",
        "/app/" + "a" * 32 + ".py extra",
    ],
)
def test_launcher_rejects_ungranted_source_paths(monkeypatch, source):
    monkeypatch.setattr(worker_run.os, "geteuid", lambda: 0)
    with pytest.raises(ValueError, match="source path"):
        worker_run.run(source, 10)


def test_launcher_requires_root(monkeypatch):
    monkeypatch.setattr(worker_run.os, "geteuid", lambda: 1000)
    with pytest.raises(PermissionError):
        worker_run.run("/app/" + "a" * 32 + ".py", 10)


def test_output_is_bounded_and_truncation_explicit(monkeypatch):
    monkeypatch.setattr(worker_run, "OUTPUT_BYTES", 4)
    assert worker_run.read_output(io.BytesIO(b"abcdef")) == ("abcd", True)
    assert worker_run.read_output(io.BytesIO(b"abcd")) == ("abcd", False)


def test_process_cleanup_fails_closed_without_pidfds(monkeypatch):
    monkeypatch.setattr(worker_run.os, "pidfd_open", None, raising=False)
    with pytest.raises(RuntimeError, match="Linux pidfd"):
        worker_run.reap_user(1000)
