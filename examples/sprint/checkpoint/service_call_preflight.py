"""Recover a real Modal function result by retained ID without dispatching again."""

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import modal


app = modal.App("sprint-call-preflight-" + uuid4().hex)


@app.function(timeout=60)
def probe():
    from uuid import uuid4

    return {"execution": uuid4().hex}


async def main(records):
    # These are operator-only dependencies; the remote probe needs only stdlib.
    from sprint.broker import ResourceBroker
    from sprint.services import ServiceContext

    records.mkdir(parents=True, exist_ok=False, mode=0o700)
    await app.deploy.aio()
    try:
        broker = ResourceBroker(
            SimpleNamespace(),
            {},
            directory=records,
            backend="modal",
            services={"evaluate": (records, "unused.py")},
        )
        context = ServiceContext(broker, "evaluate")
        first = await context.modal_call("candidate", app.name, "probe", timeout=60)
        (context.state / "candidate.call-result.json").unlink()
        restored = ServiceContext(broker, "evaluate")
        second = await restored.modal_call("candidate", app.name, "probe", timeout=60)
        if first != second:
            raise RuntimeError("recovery dispatched a different provider execution")
        # No service module is needed for this call-lifecycle-only preflight.
        from sprint.service_calls import cleanup_calls

        await cleanup_calls(restored)
        retained = json.loads((context.state / "candidate.call.json").read_text())
        if not retained.get("cancelled"):
            raise RuntimeError("provider cleanup did not retain completion")
        summary = {
            "passed": True,
            "same_execution": True,
            "call_cleanup": True,
            "scope": "Modal CPU function identity recovery; no model training.",
        }
        (records / "result.json").write_text(json.dumps(summary, indent=2))
        print(json.dumps(summary), flush=True)
    finally:
        await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "modal", "app", "stop", "--yes", app.app_id],
            check=True,
            timeout=60,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=Path, required=True)
    asyncio.run(main(parser.parse_args().records))
