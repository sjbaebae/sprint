"""Host-side learner SSH credentials; provider credentials never enter these files.

Call from the operator with directories the learner cannot replace. The published
private key grants only the worker's learner account and expires with the worker.
"""

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from sprint.file_transfer import copy_file


def create_identity(path):
    """Create a per-worker Ed25519 identity in private operator storage."""
    path = Path(path)
    if not path.exists():
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(path)],
            check=True,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=15,
        )
    return path.with_suffix(".pub").read_text().strip()


def connection_files(endpoint, identity, known_hosts):
    """Build pinned SSH files for local or sandbox-side publication."""
    host = endpoint["host"]
    port = endpoint["port"]
    host_key = endpoint["host_key"]
    if (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", host)
        or type(port) is not int
        or not 1 <= port <= 65535
        or endpoint["user"] != "agent"
        or not re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/]+=*(?: [^\r\n]+)?", host_key)
    ):
        raise ValueError("invalid learner SSH endpoint")
    hosts = f"[{host}]:{port} {host_key}\n"
    config = (
        f"Host gpu\n  HostName {host}\n  Port {port}\n  User agent\n"
        f"  IdentityFile {json.dumps(str(identity))}\n"
        f"  UserKnownHostsFile {json.dumps(str(known_hosts))}\n"
        "  StrictHostKeyChecking yes\n  IdentitiesOnly yes\n"
        "  BatchMode yes\n  ForwardAgent no\n"
    )
    return hosts, config


def publish_connection(endpoint, identity, directory, *, uid, gid):
    """Publish or refresh pinned SSH access, including a recreated worker's keys."""
    directory = Path(directory).absolute()
    hosts, content = connection_files(
        endpoint, directory / "identity", directory / "known_hosts"
    )
    directory.mkdir(mode=0o755, parents=True, exist_ok=True)
    config = directory / "config"
    with tempfile.TemporaryDirectory(prefix="sprint-connection-") as temporary:
        staged = Path(temporary).resolve()
        copy_file(identity, staged / "identity")
        (staged / "identity").chmod(0o400)
        (staged / "known_hosts").write_text(hosts)
        (staged / "config").write_text(content)
        for name in ("identity", "known_hosts", "config"):
            target = directory / name
            copy_file(staged / name, target)
            target.chmod(0o400 if name == "identity" else 0o444)
            if name == "identity":
                os.chown(target, uid, gid)
    return config
