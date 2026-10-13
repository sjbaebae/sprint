"""Bounded operator reads of learner files without symlink traversal."""

import json
import os
import stat
from pathlib import Path


def read_regular_json(path, limit, workspace=None):
    if workspace is None:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    else:
        parts = Path(path).relative_to(workspace).parts
        if not parts or any(x in (".", "..") for x in parts):
            raise ValueError("invalid workspace path")
        parent = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in parts[:-1]:
                child = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
                )
                os.close(parent)
                parent = child
            fd = os.open(
                parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
            )
        finally:
            os.close(parent)
    with os.fdopen(fd, "rb") as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            raise ValueError("regular file required")
        data = f.read(limit + 1)
        if len(data) > limit:
            raise ValueError("file too large")
        return json.loads(data)
