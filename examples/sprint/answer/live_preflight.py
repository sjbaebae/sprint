"""Prove Modal forwards stdout before a command completes, with cleanup."""

import asyncio
import json
from pathlib import Path
from uuid import uuid4

from sprint import Sandbox, Worker
from sprint.workers import WorkerSession


async def main():
    root = Path(".sprint/live-output-preflight") / uuid4().hex
    root.mkdir(parents=True)
    observed = asyncio.Event()
    journal = root / "agent-events.jsonl"

    async def record(text, stream):
        with journal.open("a") as output:
            output.write(json.dumps({"text": text, "stream": stream}) + "\n")
        if "live-output-visible" in text:
            observed.set()

    async with WorkerSession(
        Worker(
            sandbox=Sandbox(internet="none"),
            backend="modal",
            seconds=30,
            lifetime_seconds=120,
        ),
        directory=root / "worker",
    ) as worker:
        await worker.start()
        with worker.environment.scoped_output_callback(record):
            running = asyncio.create_task(
                worker.environment.exec(
                    "printf 'live-output-visible\n'; sleep 4; printf 'finished\n'",
                    user="agent",
                    timeout_sec=20,
                )
            )
            try:
                await asyncio.wait_for(observed.wait(), timeout=15)
                if running.done():
                    raise RuntimeError("Output arrived only after command completion")
                result = await running
            finally:
                if not running.done():
                    running.cancel()
                    await asyncio.gather(running, return_exceptions=True)
        if result.return_code != 0 or "finished" not in result.stdout:
            raise RuntimeError("Streaming changed execution feedback")
    print(
        json.dumps(
            {"passed": True, "observed_before_completion": True, "records": str(root)}
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
