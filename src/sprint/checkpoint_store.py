"""Private, content-addressed storage for complete logical-state checkpoints."""

import hashlib
import io
import json
import math
import os
import re
import tarfile
from pathlib import Path, PurePosixPath
from uuid import uuid4
from typing import Any

from sprint.streams import ChunkReader

CHUNK_BYTES = 4 * 1024 * 1024


def digest(data):
    return hashlib.sha256(data).hexdigest()


def safe_name(name):
    path = PurePosixPath(name)
    if not name or path.is_absolute() or ".." in path.parts or str(path) != name:
        raise ValueError(f"unsafe checkpoint path: {name}")
    return name


class CheckpointStore:
    """Deduplicate file chunks across snapshots; publish metadata atomically.

    The store is operator-owned and private. It is not a trust boundary against
    an operator who edits both manifests and hashes. No pickle is accepted.
    """

    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.blobs = self.directory / "blobs"
        self.blobs.mkdir(mode=0o700, exist_ok=True)

    def put(self, data):
        key = digest(data)
        target = self.blobs / key
        if target.exists():
            if target.is_symlink() or digest(target.read_bytes()) != key:
                raise ValueError("corrupt checkpoint blob")
            return key
        temporary = self.blobs / ("." + uuid4().hex)
        try:
            with temporary.open("xb") as stream:
                os.chmod(temporary, 0o600)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        return key

    def get(self, key):
        if not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{64}", key):
            raise ValueError("invalid checkpoint blob ID")
        target = self.blobs / key
        if target.is_symlink() or not target.is_file():
            raise ValueError("missing checkpoint blob")
        data = target.read_bytes()
        if digest(data) != key:
            raise ValueError("corrupt checkpoint blob")
        return data

    def known_files(self):
        """Index prior published file content for capture-side transfer reuse."""
        files = {}
        for path in self.directory.glob("*.json"):
            checkpoint = Checkpoint(path)
            for tree in checkpoint.metadata["trees"].values():
                self.validate_tree(tree)
                for entry in tree.values():
                    if entry["kind"] == "file":
                        files[entry["sha256"]] = entry
        return files

    def import_archive(self, archive, *, reusable=None):
        """Import a path or binary stream without extracting untrusted links.

        Stream inputs need only read(); seeking and complete archive staging are
        unnecessary. The caller owns the input stream and its transport lifetime.
        """
        entries = {}
        opened = (
            tarfile.open(fileobj=archive, mode="r|*")
            if hasattr(archive, "read")
            else tarfile.open(archive, "r:*")
        )
        with opened as stream:
            for member in stream:
                name = member.name.removeprefix("./").rstrip("/")
                if name in {"", "."}:
                    continue
                safe_name(name)
                if name in entries:
                    raise ValueError("duplicate checkpoint archive entry")
                entry: dict[str, Any] = {
                    "mode": member.mode & 0o777,
                    "uid": member.uid,
                    "gid": member.gid,
                    "mtime": member.mtime,
                }
                if member.isdir():
                    entry["kind"] = "directory"
                elif member.issym():
                    entry.update(kind="symlink", target=member.linkname)
                elif member.islnk():
                    entry.update(kind="hardlink", target=member.linkname)
                elif member.isfile() and "sprint.reuse" in member.pax_headers:
                    key = member.pax_headers["sprint.reuse"]
                    previous = (reusable or {}).get(key)
                    if previous is None or member.size != 0:
                        raise ValueError("unknown checkpoint reuse reference")
                    # Reuse must never bless missing/corrupted local content.
                    for _ in self._file_chunks(previous):
                        pass
                    entry.update(
                        kind="file",
                        size=previous["size"],
                        sha256=key,
                        chunks=list(previous["chunks"]),
                    )
                elif member.isfile():
                    file = stream.extractfile(member)
                    if file is None:
                        raise ValueError("unreadable checkpoint archive entry")
                    keys, total, checksum = [], 0, hashlib.sha256()
                    while data := file.read(CHUNK_BYTES):
                        keys.append(self.put(data))
                        total += len(data)
                        checksum.update(data)
                    if total != member.size:
                        raise ValueError("truncated checkpoint archive")
                    entry.update(
                        kind="file",
                        size=total,
                        sha256=checksum.hexdigest(),
                        chunks=keys,
                    )
                else:
                    raise ValueError(f"unsupported state file: {name}")
                entries[name] = entry
        self.validate_tree(entries)
        return entries

    @staticmethod
    def validate_tree(entries):
        if not isinstance(entries, dict):
            raise ValueError("checkpoint tree must be an object")
        for name, entry in entries.items():
            safe_name(name)
            if not isinstance(entry, dict):
                raise ValueError("checkpoint entry must be an object")
            if entry.get("kind") not in {"directory", "symlink", "hardlink", "file"}:
                raise ValueError("unknown checkpoint entry kind")
            if (
                type(entry.get("mode")) is not int
                or not 0 <= entry["mode"] <= 0o777
                or any(
                    type(entry.get(key)) is not int or entry[key] < 0
                    for key in ("uid", "gid")
                )
                or type(entry.get("mtime", 0)) not in (int, float)
                or not math.isfinite(entry.get("mtime", 0))
            ):
                raise ValueError("invalid checkpoint entry metadata")
            if entry["kind"] == "symlink" and (
                not isinstance(entry.get("target"), str) or not entry["target"]
            ):
                raise ValueError("invalid checkpoint link target")
            if entry["kind"] == "hardlink":
                target = entry.get("target")
                if not isinstance(target, str):
                    raise ValueError("invalid checkpoint hard link")
                safe_name(target)
                if entries.get(target, {}).get("kind") != "file":
                    raise ValueError(
                        "checkpoint hard link must reference a captured regular file"
                    )
            if entry["kind"] == "file" and (
                type(entry.get("size")) is not int
                or entry["size"] < 0
                or not isinstance(entry.get("sha256"), str)
                or not re.fullmatch(r"[a-f0-9]{64}", entry["sha256"])
                or not isinstance(entry.get("chunks"), list)
            ):
                raise ValueError("invalid checkpoint file metadata")
            for parent in PurePosixPath(name).parents:
                ancestor = entries.get(str(parent))
                if ancestor and ancestor["kind"] != "directory":
                    raise ValueError("checkpoint path traverses a link or file")

    def export_archive(self, entries, destination):
        """Write a path or binary stream, preserving links, modes and ownership.

        Stream outputs need only write(); data is emitted in bounded blocks.
        The caller owns the output stream and must discard partial failed exports.
        """
        self.validate_tree(entries)
        opened = (
            tarfile.open(fileobj=destination, mode="w|")
            if hasattr(destination, "write")
            else tarfile.open(destination, "w")
        )
        with opened as archive:
            # Parent directories first; links cannot have archive descendants.
            for name, entry in sorted(
                entries.items(),
                key=lambda item: (
                    item[1]["kind"] == "hardlink",
                    item[0].count("/"),
                    item[0],
                ),
            ):
                info = tarfile.TarInfo(name)
                info.mode, info.uid, info.gid = (
                    entry["mode"],
                    entry["uid"],
                    entry["gid"],
                )
                info.mtime = entry.get("mtime", 0)
                if entry["kind"] == "directory":
                    info.type = tarfile.DIRTYPE
                    archive.addfile(info)
                elif entry["kind"] == "symlink":
                    info.type, info.linkname = tarfile.SYMTYPE, entry["target"]
                    archive.addfile(info)
                elif entry["kind"] == "hardlink":
                    info.type, info.linkname = tarfile.LNKTYPE, entry["target"]
                    archive.addfile(info)
                else:
                    info.size = entry["size"]
                    with io.BufferedReader(
                        ChunkReader(self._file_chunks(entry))
                    ) as file:
                        archive.addfile(info, file)
                        # tar reads exactly the declared size. Exhaust the iterator
                        # to verify its checksum and reject undeclared trailing bytes.
                        if file.read(1):
                            raise ValueError("checkpoint file integrity failure")

    def _file_chunks(self, entry):
        """Check chunk hashes and the complete file while streaming an export."""
        checksum, total = hashlib.sha256(), 0
        for key in entry["chunks"]:
            data = self.get(key)
            total += len(data)
            if total > entry["size"]:
                raise ValueError("checkpoint file integrity failure")
            checksum.update(data)
            yield data
        if total != entry["size"] or checksum.hexdigest() != entry["sha256"]:
            raise ValueError("checkpoint file integrity failure")

    def ensure_new_name(self, name):
        """Validate before pausing writers; publication still refuses races."""
        if not isinstance(name, str) or not re.fullmatch(
            r"[a-zA-Z0-9][a-zA-Z0-9_-]*", name
        ):
            raise ValueError("checkpoint name must be a simple identifier")
        target = self.directory / (name + ".json")
        if target.exists():
            raise FileExistsError(target)
        return target

    def publish(self, name, metadata):
        target = self.ensure_new_name(name)
        payload = {"schema_version": 1, **metadata}
        canonical = json.dumps(payload, sort_keys=True, allow_nan=False).encode()
        payload["manifest_sha256"] = digest(canonical)
        temporary = self.directory / ("." + uuid4().hex)
        try:
            with temporary.open("xb") as stream:
                os.chmod(temporary, 0o600)
                stream.write(json.dumps(payload, indent=2, allow_nan=False).encode())
                stream.flush()
                os.fsync(stream.fileno())
            # Hard-link publication is atomic and refuses overwrite races.
            os.link(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return Checkpoint(target)


class Checkpoint:
    """A complete logical checkpoint referencing shared immutable file chunks."""

    def __init__(self, path):
        self.path = Path(path).resolve(strict=True)
        if not self.path.is_file():
            raise ValueError("checkpoint must be a manifest file")
        self.store = CheckpointStore(self.path.parent)
        self.metadata = json.loads(self.path.read_text())
        expected = self.metadata.pop("manifest_sha256", None)
        actual = digest(
            json.dumps(self.metadata, sort_keys=True, allow_nan=False).encode()
        )
        if expected != actual or self.metadata.get("schema_version") != 1:
            raise ValueError("checkpoint manifest integrity failure")

    def verify(self):
        """Check every referenced blob and file before provisioning a restore."""
        for tree in self.metadata["trees"].values():
            self.store.validate_tree(tree)
            for entry in tree.values():
                if entry["kind"] != "file":
                    continue
                total, checksum = 0, hashlib.sha256()
                for key in entry["chunks"]:
                    data = self.store.get(key)
                    total += len(data)
                    checksum.update(data)
                if total != entry["size"] or checksum.hexdigest() != entry["sha256"]:
                    raise ValueError("checkpoint file integrity failure")
        return self

    def diff(self, other):
        """Return added/deleted/modified paths across logical state trees."""
        other = other if isinstance(other, Checkpoint) else Checkpoint(other)
        changes = []
        before, after = self.metadata["trees"], other.metadata["trees"]
        for tree in sorted(before.keys() | after.keys()):
            a, b = before.get(tree, {}), after.get(tree, {})
            for path in sorted(a.keys() | b.keys()):
                if a.get(path) == b.get(path):
                    continue
                changes.append(
                    {
                        "tree": tree,
                        "path": path,
                        "status": "added"
                        if path not in a
                        else "deleted"
                        if path not in b
                        else "modified",
                    }
                )
        return changes
