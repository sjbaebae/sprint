"""Bounded, inspectable records of learner files without following symlinks."""

import hashlib
import os
import stat
from pathlib import Path


def workspace_evidence(
    workspace,
    content_budget=16 * 1024 * 1024,
    *,
    exclude_dirs=(".venv", "__pycache__", ".git"),
):
    """Retain text within a byte budget and hashes for omitted binary/large files."""
    if type(content_budget) is not int or content_budget < 0:
        raise ValueError("content_budget must be a nonnegative integer")
    root = Path(workspace)
    files = []
    remaining = content_budget
    for directory, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(
            d
            for d in dirs
            if d not in exclude_dirs and not (Path(directory) / d).is_symlink()
        )
        for name in sorted(names):
            path = Path(directory) / name
            entry: dict[str, object] = {"path": str(path.relative_to(root))}
            if path.is_symlink():
                entry["omitted"] = "symlink"
                files.append(entry)
                continue
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                meta = os.fstat(stream.fileno())
                if not stat.S_ISREG(meta.st_mode):
                    entry["omitted"] = "non-regular file"
                    files.append(entry)
                    continue
                digest = hashlib.sha256()
                size = 0
                parts = []
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
                    size += len(block)
                    if size <= min(1024 * 1024, remaining):
                        parts.append(block)
                entry.update(bytes=size, sha256=digest.hexdigest())
                if size > 1024 * 1024 or size > remaining:
                    entry["omitted"] = "large file; hash retained"
                else:
                    try:
                        entry["text"] = b"".join(parts).decode("utf-8")
                        remaining -= size
                    except UnicodeDecodeError:
                        entry["omitted"] = "binary file; hash retained"
            files.append(entry)
    return {
        "content_budget_bytes": content_budget,
        "content_bytes": content_budget - remaining,
        "files": files,
    }
