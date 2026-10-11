"""Root-owned, stdlib-only safe snapshot of one worker checkpoint."""

import hashlib
import json
import os
from pathlib import PurePosixPath
import stat
import sys
import tempfile

LIMIT = 1024 * 1024 * 1024
ROOT = "/app"


def snapshot(relative):
    """Copy a bounded regular file beneath /app, rejecting every symlink hop."""
    path = PurePosixPath(relative)
    if not path.parts or path.is_absolute() or ".." in path.parts:
        raise ValueError("checkpoint path must be relative to /app")
    directory = os.open(ROOT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = None
    try:
        for part in path.parts[:-1]:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
            )
            os.close(directory)
            directory = child
        descriptor = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
        with os.fdopen(descriptor, "rb") as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > LIMIT:
                raise ValueError("checkpoint must be a regular file of at most 1 GiB")
            digest = hashlib.sha256()
            total = 0
            with tempfile.NamedTemporaryFile(
                prefix="worlds-export-", delete=False
            ) as target:
                temporary = target.name
                while block := source.read(1024 * 1024):
                    total += len(block)
                    if total > LIMIT:
                        raise ValueError("checkpoint exceeds 1 GiB")
                    digest.update(block)
                    target.write(block)
            after = os.fstat(source.fileno())
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise ValueError("checkpoint changed during collection")
        result = {"path": temporary, "sha256": digest.hexdigest(), "bytes": total}
        temporary = None
        return result
    finally:
        os.close(directory)
        if temporary is not None:
            os.unlink(temporary)


if __name__ == "__main__":
    print(json.dumps(snapshot(sys.argv[1])))
