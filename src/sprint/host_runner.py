"""Private SSH entrypoint; provider credentials stay on the selected host."""

import asyncio
import json
import os
import signal
import sys
from pathlib import Path
from typing import Any

from .spec import (
    Agent,
    Budget,
    Env,
    Experiment,
    Files,
    Resources,
    Sandbox,
    Task,
    Validation,
    Worker,
)


def decode(value: dict[str, Any]):
    """Reconstruct only Sprint's declarative definitions; never deserialize code."""

    def sandbox(data):
        return Sandbox(**data)

    def worker(data):
        converted: dict[str, Any] = dict(data)
        converted["sandbox"] = sandbox(data["sandbox"])
        converted["resources"] = Resources(**data["resources"])
        return Worker(**converted)

    env = value["env"]
    attachments = {
        name: worker(item) if "seconds" in item else Files(Path(item["path"]))
        for name, item in env["attachments"].items()
    }
    validation: dict[str, Any] = dict(value["validation"])
    validation["sandbox"] = sandbox(validation["sandbox"])
    validation["resources"] = Resources(**validation["resources"])
    converted_env: dict[str, Any] = dict(env)
    converted_env["sandbox"] = sandbox(env["sandbox"])
    converted_env["resources"] = Resources(**env["resources"])
    converted_env["attachments"] = attachments
    return Experiment(
        Task(**value["task"]),
        Validation(**validation),
        Agent(**value["agent"]),
        Env(**converted_env),
        Budget(**value["budget"]),
    )


async def main(request):
    from .backends.runtime import run

    root = request.parent
    payload = json.loads(request.read_text())
    os.environ.update(payload["env"])
    output = root / "output"
    output.mkdir(mode=0o700)
    identity = {
        "pid": os.getpid(),
        "start": Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()[19],
    }
    (root / "process.json").write_text(json.dumps(identity))
    current = asyncio.current_task()
    if current is None:
        raise RuntimeError("host runner requires an async task")
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        loop.add_signal_handler(signum, current.cancel)
    try:
        await run(
            decode(payload["experiment"]),
            directory=output,
            backend=payload["backend"],
            trials=payload["trials"],
            parallel=payload["parallel"],
            compiled_task=root / "task" if "checkpoint" not in payload else None,
            resume_from=root / "checkpoints" / payload["checkpoint"]
            if "checkpoint" in payload
            else None,
        )
    finally:
        (root / "process.json").unlink(missing_ok=True)


def stop(request):
    """Cancel only this run's still-identical process; normal cleanup owns resources."""
    path = request.parent / "process.json"
    if not path.is_file():
        return
    identity = json.loads(path.read_text())
    process = Path("/proc") / str(identity["pid"])
    try:
        started = (process / "stat").read_text().rsplit(")", 1)[1].split()[19]
        if started == identity["start"]:
            os.kill(identity["pid"], signal.SIGTERM)
    except (ProcessLookupError, FileNotFoundError):
        return
    for _ in range(50):
        if not path.exists():
            return
        import time

        time.sleep(1)
    raise RuntimeError(
        "host cancellation cleanup has not completed; inspect remote run"
    )


if __name__ == "__main__":
    request = Path(sys.argv[1]).resolve()
    if "--stop" in sys.argv:
        stop(request)
    else:
        asyncio.run(main(request))
