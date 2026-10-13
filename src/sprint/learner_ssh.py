"""Host-side learner SSH credentials; provider credentials never enter these files.

Call from the operator with directories the learner cannot replace. The published
private key grants only the worker's learner account and expires with the worker.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path


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
    """Publish pinned SSH/SCP access; only the identity is learner-owned."""
    directory = Path(directory)
    hosts, content = connection_files(
        endpoint, directory / "identity", directory / "known_hosts"
    )
    directory.mkdir(mode=0o755, exist_ok=True)
    config = directory / "config"
    if config.exists():
        return config
    published_identity = directory / "identity"
    shutil.copyfile(identity, published_identity)
    published_identity.chmod(0o400)
    os.chown(published_identity, uid, gid)
    known_hosts = directory / "known_hosts"
    known_hosts.write_text(hosts)
    known_hosts.chmod(0o444)
    config.write_text(content)
    config.chmod(0o444)
    return config
