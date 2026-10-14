"""Trusted task handlers sharing the native broker's state and resource ownership."""

import importlib.util
import inspect
import sys
from pathlib import Path
from uuid import uuid4


class ServiceContext:
    """Operator-only state and access to this trial's granted workers."""

    def __init__(self, broker, name):
        self._broker = broker
        self.state = broker.directory / ("service-" + name)
        self.state.mkdir(mode=0o700, exist_ok=True)

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
        return handle
    except BaseException:
        sys.modules.pop(name, None)
        raise
