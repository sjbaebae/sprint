"""Root-only logical-state transfer helper; uploaded by the operator, not the agent."""

import json
import os
import pwd
import signal
import stat
import sys
import tarfile
from pathlib import Path


def identity(pid):
    path = Path("/proc") / str(pid)
    # Start time guards against PID reuse when unfreezing the original run.
    return path.stat().st_uid, (path / "stat").read_text().rsplit(")", 1)[1].split()[19]


def freeze():
    uid = pwd.getpwnam("agent").pw_uid
    stopped = {}
    for _ in range(8):
        found = False
        for path in Path("/proc").iterdir():
            if not path.name.isdigit():
                continue
            pid = int(path.name)
            try:
                owner, started = identity(pid)
                if owner == uid and pid not in stopped:
                    os.kill(pid, signal.SIGSTOP)
                    stopped[pid] = started
                    found = True
            except (ProcessLookupError, FileNotFoundError):
                pass
        if not found:
            return stopped
    unfreeze(stopped)
    raise RuntimeError("could not quiesce agent process tree")


def unfreeze(stopped):
    uid = pwd.getpwnam("agent").pw_uid
    for pid, started in stopped.items():
        try:
            pid = int(pid)
            if identity(pid) == (uid, started):
                os.kill(pid, signal.SIGCONT)
        except (ProcessLookupError, FileNotFoundError):
            pass


def validate_roots(roots):
    """Never resolve a restored root through an existing parent symlink."""
    for root in roots:
        for parent in Path(root).parents:
            if parent.is_symlink():
                raise ValueError(f"state root traverses a symlink: {root}")


def pack(roots, destination):
    """Refuse omissions: sockets/devices/pipes require explicit application recovery."""
    validate_roots(roots)
    present = []
    for root in roots:
        path = Path(root)
        if path.is_symlink() or not path.is_dir():
            if not path.exists() and not path.is_symlink():
                continue
            raise ValueError(f"state root must be a directory: {root}")
        for base, dirs, names in os.walk(path, followlinks=False):
            for name in dirs + names:
                file = Path(base) / name
                mode = file.lstat().st_mode
                if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode) or stat.S_ISLNK(mode)):
                    raise ValueError(f"nonportable state file: {file}")
        present.append(root)
    with open(destination, "xb") as output:
        os.chmod(destination, 0o600)
        with tarfile.open(fileobj=output, mode="w", dereference=False) as archive:
            for root in present:
                excluded = {Path(destination).absolute(), Path(__file__).absolute()}

                def keep(entry):
                    return None if Path("/" + entry.name) in excluded else entry

                archive.add(root, arcname=root.lstrip("/"), filter=keep)
    return {"roots": present, "bytes": Path(destination).stat().st_size}


if __name__ == "__main__":
    action = sys.argv[1]
    if action == "freeze":
        print(json.dumps(freeze()))
    elif action == "unfreeze":
        unfreeze(json.loads(sys.argv[2]))
    elif action == "validate-roots":
        validate_roots(json.loads(sys.argv[2]))
    elif action == "pack":
        print(json.dumps(pack(json.loads(sys.argv[2]), sys.argv[3])))
    else:
        raise ValueError("unknown state operation")
