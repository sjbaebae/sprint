"""Bounded workspace observations for operator-side inspection.

The collector uses only stdlib and can run unchanged in the GPU sandbox. It
never executes learner files or follows links. Skips are coverage gaps, not passes.
"""

import hashlib
import json
import os
import socket
import sys
import stat
import time
from pathlib import Path


def network_snapshot(proc=Path("/proc/net"), learner_uid=1000):
    """Sample learner sockets; short-lived connections and TLS content are absent."""
    records = []
    errors = []

    def endpoint(value, family):
        address, port = value.split(":")
        packed = b"".join(
            int(address[offset : offset + 8], 16).to_bytes(4, sys.byteorder)
            for offset in range(0, len(address), 8)
        )
        return {"address": socket.inet_ntop(family, packed), "port": int(port, 16)}

    for protocol, family in (
        ("tcp", socket.AF_INET),
        ("tcp6", socket.AF_INET6),
        ("udp", socket.AF_INET),
        ("udp6", socket.AF_INET6),
    ):
        try:
            for line in (proc / protocol).read_text().splitlines()[1:5001]:
                fields = line.split()
                if int(fields[7]) != learner_uid:
                    continue
                records.append(
                    {
                        "protocol": protocol,
                        "local": endpoint(fields[1], family),
                        "remote": endpoint(fields[2], family),
                        "state_hex": fields[3],
                    }
                )
        except (OSError, ValueError, IndexError) as error:
            errors.append({"protocol": protocol, "error": str(error)})
    return {
        "observed_at_unix": time.time(),
        "learner_uid": learner_uid,
        "connections": records,
        "errors": errors,
        "scope": "sampled sockets only; no DNS, process attribution, byte counts or payloads",
    }


def collect(
    root,
    *,
    max_bytes=8 * 1024**2,
    max_file_bytes=2 * 1024**2,
    seconds=5,
    start_after="",
):
    started = time.monotonic()
    records = []
    consumed = 0
    complete = not start_after
    stopped = False
    last_path = start_after

    def walk(directory, relative):
        nonlocal consumed, complete, stopped, last_path
        for name in sorted(os.listdir(directory)):
            path = relative + "/" + name
            if (
                stopped
                or time.monotonic() - started > seconds
                or consumed >= max_bytes
                or len(records) >= 5000
            ):
                complete = False
                stopped = True
                return
            fd = None
            try:
                fd = os.open(
                    name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
                )
                before = os.fstat(fd)
                if stat.S_ISDIR(before.st_mode):
                    walk(fd, path)
                    continue
                if tuple(path.split("/")) <= tuple(start_after.split("/")):
                    continue
                if (
                    stat.S_ISREG(before.st_mode)
                    and before.st_size <= max_file_bytes
                    and consumed + before.st_size > max_bytes
                ):
                    complete = False
                    stopped = True
                    return
                last_path = path
                if not stat.S_ISREG(before.st_mode):
                    records.append(
                        {
                            "path": path,
                            "status": "skipped",
                            "reason": "not a regular file",
                        }
                    )
                    continue
                if before.st_size > max_file_bytes:
                    records.append(
                        {
                            "path": path,
                            "status": "skipped",
                            "reason": "size bound",
                            "bytes": before.st_size,
                        }
                    )
                    complete = False
                    continue
                data = b""
                while len(data) <= max_file_bytes:
                    block = os.read(fd, min(65536, max_file_bytes + 1 - len(data)))
                    if not block:
                        break
                    data += block
                consumed += len(data)
                after = os.fstat(fd)
                if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                ) or len(data) != before.st_size:
                    raise ValueError("file changed while reading")
                row = {
                    "path": path,
                    "bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
                try:
                    row.update(status="captured", text=data.decode("utf-8"))
                except UnicodeDecodeError:
                    row.update(status="skipped", reason="not UTF-8 text")
                    complete = False
                records.append(row)
            except (OSError, ValueError) as error:
                if tuple(path.split("/")) <= tuple(start_after.split("/")):
                    continue
                last_path = path
                complete = False
                records.append({"path": path, "status": "error", "reason": str(error)})
            finally:
                if fd is not None:
                    os.close(fd)

    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        walk(fd, "")
    finally:
        os.close(fd)
    return {
        "root": str(root),
        "complete": complete,
        "bytes_read": consumed,
        "start_after": start_after,
        "next_cursor": last_path if stopped else "",
        "duration_seconds": time.monotonic() - started,
        "files": records,
        "network": network_snapshot(),
    }


def file_changes(payload, previous):
    """Update known hashes; infer deletions only after a complete observation."""
    added = []
    modified = []
    observed = {row["path"] for row in payload["files"]}
    for row in payload["files"]:
        fingerprint = row.get("sha256")
        if fingerprint is None:
            continue
        path = row["path"]
        if path not in previous:
            added.append(path)
        elif previous[path] != fingerprint:
            modified.append(path)
        previous[path] = fingerprint
    deleted = []
    if payload["complete"]:
        deleted = sorted(set(previous) - observed)
        for path in deleted:
            del previous[path]
    return {
        "added": added,
        "modified": modified,
        "deleted": deleted,
        "deletions_checked": payload["complete"],
    }


def collector_source():
    """Return stdlib-only collector source for an isolated remote Python process."""
    return Path(__file__).read_text()


if __name__ == "__main__":
    print(
        json.dumps(
            collect(sys.argv[1], start_after=sys.argv[2] if len(sys.argv) > 2 else "")
        )
    )
