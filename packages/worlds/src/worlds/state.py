"""Operator-side file manifests and bounded text diffs, independent of the backend."""

import difflib
import hashlib
import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class StateTracker:
    """Snapshot a quiescent exported workspace into a separate private directory.

    This is an inspection utility, not a sandbox or an atomic filesystem snapshot.
    Run it on operator-owned exports. Do not scan live untrusted filesystem trees:
    path ancestors could change during traversal. Backend hooks for per-call
    workspace exports are intentionally separate from this utility.
    """

    def __init__(
        self,
        workspace,
        store,
        *,
        max_file_bytes=16 * 1024 * 1024,
        text_bytes=128 * 1024,
    ):
        self.workspace = Path(workspace).resolve(strict=True)
        self.store = Path(store).resolve()
        if not self.workspace.is_dir():
            raise ValueError("workspace must be a directory")
        if self.store.is_relative_to(self.workspace) or self.workspace.is_relative_to(
            self.store
        ):
            raise ValueError("snapshot store and workspace must be separate trees")
        if max_file_bytes <= 0 or text_bytes < 0:
            raise ValueError("invalid snapshot limits")
        self.max_file_bytes = max_file_bytes
        self.text_bytes = text_bytes
        self.store.mkdir(mode=0o700, parents=True, exist_ok=True)

    def capture(self, label, *, turn=None):
        """Store manifest and bounded text; record all omissions explicitly."""
        if not label or Path(label).name != label or label in {".", ".."}:
            raise ValueError("snapshot label must be a filename")
        destination = self.store / label
        destination.mkdir(mode=0o700, exist_ok=False)
        files = {}
        for base, dirs, names in os.walk(self.workspace, followlinks=False):
            for name in sorted(dirs + names):
                path = Path(base) / name
                relative = path.relative_to(self.workspace).as_posix()
                info = path.lstat()
                record: dict[str, Any] = {"mode": stat.S_IMODE(info.st_mode)}
                if stat.S_ISLNK(info.st_mode):
                    record.update(kind="symlink", target=os.readlink(path))
                elif stat.S_ISDIR(info.st_mode):
                    continue
                elif not stat.S_ISREG(info.st_mode):
                    record.update(kind="special", omitted="not a regular file")
                elif info.st_size > self.max_file_bytes:
                    record.update(
                        kind="file",
                        size=info.st_size,
                        omitted="exceeds snapshot file-size limit",
                    )
                else:
                    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    with os.fdopen(fd, "rb") as stream:
                        before = os.fstat(stream.fileno())
                        if not stat.S_ISREG(before.st_mode):
                            raise ValueError("workspace changed during capture")
                        data = stream.read(self.max_file_bytes + 1)
                        after = os.fstat(stream.fileno())
                    if len(data) > self.max_file_bytes or (
                        before.st_size,
                        before.st_mtime_ns,
                    ) != (after.st_size, after.st_mtime_ns):
                        raise ValueError("file changed during capture")
                    record.update(
                        kind="file",
                        size=len(data),
                        sha256=hashlib.sha256(data).hexdigest(),
                    )
                    if len(data) <= self.text_bytes and b"\x00" not in data:
                        try:
                            record["text"] = data.decode("utf-8")
                        except UnicodeDecodeError:
                            pass
                files[relative] = record
        result = {
            "label": label,
            "turn": turn,
            "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "files": files,
        }
        (destination / "snapshot.json").write_text(json.dumps(result, indent=2))
        return result

    @staticmethod
    def diff(before, after):
        """Report added/deleted/changed files with text patches when retained."""
        changes = []
        for path in sorted(before["files"].keys() | after["files"].keys()):
            old, new = before["files"].get(path), after["files"].get(path)
            if (old and old.get("omitted")) or (new and new.get("omitted")):
                changes.append(
                    {
                        "path": path,
                        "status": "unverified",
                        "reason": "content was omitted from a snapshot",
                    }
                )
                continue
            if old == new:
                continue
            change = {
                "path": path,
                "status": "added"
                if old is None
                else "deleted"
                if new is None
                else "modified",
            }
            if (old is None or "text" in old) and (new is None or "text" in new):
                change["patch"] = "".join(
                    difflib.unified_diff(
                        (old or {}).get("text", "").splitlines(True),
                        (new or {}).get("text", "").splitlines(True),
                        fromfile="before/" + path,
                        tofile="after/" + path,
                    )
                )
            changes.append(change)
        return changes
