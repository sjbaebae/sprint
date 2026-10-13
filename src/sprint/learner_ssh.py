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


def publish_connection(endpoint, identity, directory, *, uid, gid):
    """Publish a host-key-pinned SSH/SCP config under the alias `gpu`.

    The operator owns the directory and config. Only the copied identity is owned
    by the learner. Repeated calls reuse the same immutable connection files.
    """
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
    directory = Path(directory)
    directory.mkdir(mode=0o755, exist_ok=True)
    config = directory / "config"
    if config.exists():
        return config
    published_identity = directory / "identity"
    shutil.copyfile(identity, published_identity)
    published_identity.chmod(0o400)
    os.chown(published_identity, uid, gid)
    known_hosts = directory / "known_hosts"
    known_hosts.write_text(f"[{host}]:{port} {host_key}\n")
    known_hosts.chmod(0o444)
    config.write_text(
        f"Host gpu\n  HostName {host}\n  Port {port}\n  User agent\n"
        f"  IdentityFile {json.dumps(str(published_identity))}\n"
        f"  UserKnownHostsFile {json.dumps(str(known_hosts))}\n"
        "  StrictHostKeyChecking yes\n  IdentitiesOnly yes\n"
        "  BatchMode yes\n  ForwardAgent no\n"
    )
    config.chmod(0o444)
    return config
