"""Stdlib-only agent client and bounded queue reader, installed root-owned."""

import argparse
import base64
import json
import os
from pathlib import Path
import re
import stat
import time
from uuid import uuid4

ROOT = Path("/resources/control")
LIMIT = 2 * 1024 * 1024


def read_requests():
    """Read pending regular files; acknowledge only atomically published feedback.

    A lost provider response must leave valid requests available for another read.
    Malformed inputs are discarded without following links or blocking on FIFOs.
    """
    requests = []
    directory = os.open(ROOT / "requests", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        names = sorted(
            name
            for name in os.listdir(directory)
            if re.fullmatch(r"[a-f0-9]{32}\.json", name)
        )
        for name in names[:32]:
            remove = False
            try:
                # Feedback is root-owned and atomically published by the host.
                # Reading is not acknowledgment: retain valid requests until then.
                if (ROOT / "results" / name).is_file():
                    remove = True
                    continue
                fd = os.open(
                    name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
                )
                with os.fdopen(fd, "rb") as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_size > LIMIT:
                        remove = True
                        continue
                    payload = json.loads(stream.read(LIMIT + 1))
                requests.append({"request_id": name[:-5], "payload": payload})
            except (OSError, ValueError):
                remove = True
            finally:
                if remove:
                    try:
                        os.unlink(name, dir_fd=directory)
                    except OSError:
                        pass
    finally:
        os.close(directory)
    return requests


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "operation",
        choices=[
            "list",
            "run",
            "exec",
            "connect",
            "upload",
            "collect",
            "call",
            "status",
            "_read",
        ],
    )
    parser.add_argument("target", nargs="?")
    parser.add_argument("script", nargs="?")
    parser.add_argument("destination", nargs="?")
    parser.add_argument("--wait", type=float, default=0)
    args = parser.parse_args()
    if args.operation == "_read":
        print(json.dumps(read_requests()))
        return
    if args.operation == "list":
        print((ROOT / "manifest.json").read_text())
        return
    if args.operation in {"run", "exec", "connect", "upload", "collect", "call"}:
        if not args.target or (args.operation != "connect" and not args.script):
            parser.error(
                "connect requires a target; run/exec/upload/collect also requires source/command/path"
            )
        if args.operation == "call":
            payload = {
                "service": args.target,
                "input": json.loads(Path(args.script).read_text()),
            }
        elif args.operation == "connect":
            payload = {"resource": args.target, "connect": True}
        elif args.operation == "run":
            payload = {"resource": args.target, "code": Path(args.script).read_text()}
        elif args.operation == "exec":
            payload = {"resource": args.target, "command": args.script}
        elif args.operation == "collect":
            payload = {"collect": args.target, "path": args.script}
        else:  # upload
            if not args.destination:
                parser.error("upload requires RESOURCE LOCAL_FILE RELATIVE_DESTINATION")
            with Path(args.script).open("rb") as stream:
                content = stream.read(1024 * 1024 + 1)
            if len(content) > 1024 * 1024:
                parser.error(
                    "upload exceeds 1 MiB; use Files attachments for large datasets"
                )
            payload = {
                "resource": args.target,
                "content": base64.b64encode(content).decode(),
                "path": args.destination,
            }
        request_id = uuid4().hex
        encoded = json.dumps(payload).encode()
        if len(encoded) > LIMIT:
            parser.error("request exceeds 2 MiB")
        temporary = ROOT / "requests" / (request_id + ".tmp")
        temporary.write_bytes(encoded)
        temporary.rename(temporary.with_suffix(".json"))
    else:
        request_id = args.target or ""
        if not re.fullmatch(r"[a-f0-9]{32}", request_id):
            parser.error("invalid request ID")
    end = time.monotonic() + max(0, args.wait)
    response = ROOT / "results" / (request_id + ".json")
    while not response.exists() and time.monotonic() < end:
        time.sleep(0.5)
    print(
        response.read_text()
        if response.exists()
        else json.dumps({"request_id": request_id, "status": "pending"})
    )


if __name__ == "__main__":
    main()
