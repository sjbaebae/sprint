"""Live offline software execution; scoring stays on the calling host."""

import argparse
import asyncio
import json
from pathlib import Path
from uuid import uuid4

from sprint import SoftwareSession


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=("modal", "docker"), default="modal")
    args = parser.parse_args()
    directory = Path(".sprint/software-preflight") / uuid4().hex
    submission = directory / "submission"
    submission.mkdir(parents=True)
    (submission / "main.py").write_text(
        "import json, socket, sys\n"
        "row = json.load(sys.stdin)\n"
        "try:\n socket.create_connection(('1.1.1.1', 443), timeout=3)\n"
        "except OSError:\n blocked = True\n"
        "else:\n blocked = False\n"
        "print(json.dumps({'value': sum(row['values']), 'offline': blocked}))\n"
    )
    async with SoftwareSession(
        submission, directory=directory / "worker", backend=args.backend, seconds=180
    ) as session:
        result = await session.run(
            ["python", "main.py"], stdin=json.dumps({"values": [6, 7]}).encode()
        )
        value = json.loads(result["stdout"])
        if result["return_code"] != 0 or value != {"value": 13, "offline": True}:
            raise RuntimeError("isolated software result did not match host scoring")
        failed = await session.run(["python", "-c", "raise SystemExit(7)"])
        if failed["return_code"] != 7:
            raise RuntimeError("nonzero software exit was not preserved")
    if not session.worker.closed:
        raise RuntimeError("software worker was not closed")
    print(json.dumps({"passed": True, "records": str(directory)}))


if __name__ == "__main__":
    asyncio.run(main())
