"""Trusted task handlers sharing the native broker's state and resource ownership."""

import importlib.util
import inspect
import json
from datetime import UTC, datetime
import sys
from pathlib import Path
from uuid import uuid4


class ServiceContext:
    """Operator-only state and access to this trial's granted workers."""

    def __init__(self, broker, name):
        self._broker = broker
        self.name = name
        self.state = broker.directory / ("service-" + name)
        self.state.mkdir(mode=0o700, exist_ok=True)

    def record(self, event, payload):
        """Publish deliberate operator evidence to verifier records and run review.

        Private working files remain checkpointed but excluded from this channel.
        The handler must redact protected examples and credentials before recording.
        """
        if not isinstance(event, str) or not event.strip():
            raise ValueError("service evidence event must be a nonempty string")
        value = {
            "timestamp": datetime.now(UTC).isoformat(),
            "service": self.name,
            "event": event,
            "payload": payload,
        }
        encoded = json.dumps(value, allow_nan=False)
        if len(encoded.encode()) > 2 * 1024 * 1024:
            raise ValueError("service evidence exceeds 2 MiB")
        journal = self._broker.directory / ("service-" + self.name + ".jsonl")
        with journal.open("a") as stream:
            stream.write(encoded + "\n")

    async def modal_call(self, key, app, function, *args, timeout=300, **kwargs):
        """Dispatch/rejoin an owned Modal call using a stable task operation key.

        Inputs/results must be bounded JSON. Keys and call identities survive
        checkpoints. Unknown launch outcomes fail closed instead of launching twice.
        """
        from .service_calls import modal_call

        return await modal_call(self, key, app, function, args, kwargs, timeout)

    async def worker(self, name):
        """Use broker-owned sessions; their deadlines, spending and cleanup apply."""
        if name not in self._broker.workers:
            raise ValueError("worker is not granted to this trial")
        return await self._broker.session(name)


def load_handler(bundle, entrypoint):
    """Load only compiled operator code; relative helper imports stay isolated."""
    bundle = Path(bundle)
    name = "_sprint_service_" + uuid4().hex
    spec = importlib.util.spec_from_file_location(
        name, bundle / entrypoint, submodule_search_locations=[str(bundle)]
    )
    if spec is None or spec.loader is None:
        raise ValueError("service entrypoint is not importable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        handle = getattr(module, "handle", None)
        if not inspect.iscoroutinefunction(handle):
            raise ValueError("service must export async handle(context, request)")
        finalize = getattr(module, "finalize", None)
        if finalize is not None and not inspect.iscoroutinefunction(finalize):
            raise ValueError("service finalize(context) must be async")
        setattr(handle, "finalize", finalize)
        return handle
    except BaseException:
        sys.modules.pop(name, None)
        raise
