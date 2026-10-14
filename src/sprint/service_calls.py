"""Durable identities for Modal calls dispatched by trusted task services."""

import asyncio
import hashlib
import json
import math
import re


def write_record(path, value):
    """Publish private records atomically before advancing provider operations."""
    encoded = json.dumps(value, allow_nan=False)
    if len(encoded.encode()) > 2 * 1024 * 1024:
        raise ValueError("service call record exceeds 2 MiB")
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(encoded)
    temporary.chmod(0o600)
    temporary.replace(path)


async def modal_call(context, key, app, function, args, kwargs, timeout):
    """Await one retained call; never replay an uncertain dispatch."""
    if not isinstance(key, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", key):
        raise ValueError("invalid service call key")
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("service call timeout must be finite and positive")
    gate = context._broker.service_call_gates.setdefault(
        (context.name, key), asyncio.Lock()
    )
    async with asyncio.timeout(timeout):
        async with gate:
            return await _modal_call(context, key, app, function, args, kwargs, timeout)


async def _modal_call(context, key, app, function, args, kwargs, timeout):
    import modal

    deadline = asyncio.get_running_loop().time() + timeout
    definition = json.dumps(
        [app, function, args, kwargs], allow_nan=False, sort_keys=True
    )
    digest = hashlib.sha256(definition.encode()).hexdigest()
    record_path = context.state / (key + ".call.json")
    result_path = context.state / (key + ".call-result.json")
    if record_path.exists():
        record = json.loads(record_path.read_text())
        if record["definition_sha256"] != digest:
            raise ValueError("service call key was already used for different inputs")
        if result_path.exists():
            return json.loads(result_path.read_text())
        if record.get("cancelled"):
            raise RuntimeError(
                "retained service call was cancelled; it was not replayed"
            )
        if not record.get("call_id"):
            raise RuntimeError(
                "service call launch outcome is unknown; it was not replayed"
            )
        call = modal.FunctionCall.from_id(record["call_id"])
    else:
        if context._broker.closing:
            raise RuntimeError("cannot dispatch service calls during shutdown")
        record = {
            "definition_sha256": digest,
            "terminate_containers": True,
            "dispatch_pending": True,
        }
        write_record(record_path, record)
        async with asyncio.timeout_at(deadline):
            call = await modal.Function.from_name(app, function).spawn.aio(
                *args, **kwargs
            )
        record["call_id"] = call.object_id
        record["dispatch_pending"] = False
        write_record(record_path, record)
    async with asyncio.timeout_at(deadline):
        result = await call.get.aio(
            timeout=max(0, deadline - asyncio.get_running_loop().time())
        )
    write_record(result_path, result)
    return result


async def cleanup_calls(context):
    """Recover owned calls and temporary artifact volumes through one cleanup path."""
    from .modal_worker import cleanup_resources

    await asyncio.to_thread(cleanup_resources, context.state)
