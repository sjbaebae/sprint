"""Learner credentials pin the host without exposing an operator identity."""

import pytest

from sprint import learner_ssh


def test_published_connection_is_pinned_and_reusable(tmp_path, monkeypatch):
    monkeypatch.setattr(learner_ssh.os, "chown", lambda *args: None)
    identity = tmp_path / "private-key"
    identity.write_text("synthetic learner key")
    endpoint = {
        "host": "gpu.example",
        "port": 22223,
        "user": "agent",
        "host_key": "ssh-ed25519 AAAA",
    }
    directory = tmp_path / "published"
    config = learner_ssh.publish_connection(
        endpoint, identity, directory, uid=1000, gid=1000
    )
    assert "StrictHostKeyChecking yes" in config.read_text()
    assert (directory / "identity").stat().st_mode & 0o777 == 0o400
    assert (
        directory / "known_hosts"
    ).read_text() == "[gpu.example]:22223 ssh-ed25519 AAAA\n"
    assert (
        learner_ssh.publish_connection(
            endpoint, identity, directory, uid=1000, gid=1000
        )
        == config
    )
    identity.write_text("new synthetic learner key")
    learner_ssh.publish_connection(
        dict(endpoint, host="restored.example", host_key="ssh-ed25519 AAAB"),
        identity,
        directory,
        uid=1000,
        gid=1000,
    )
    assert "HostName restored.example" in config.read_text()
    assert (
        directory / "known_hosts"
    ).read_text() == "[restored.example]:22223 ssh-ed25519 AAAB\n"
    assert (directory / "identity").read_text() == "new synthetic learner key"
    with pytest.raises(ValueError, match="endpoint"):
        learner_ssh.publish_connection(
            dict(endpoint, user="root"), identity, directory, uid=1000, gid=1000
        )
