"""Per-trial service failures must not strand or cancel independent agents."""

import asyncio
import json
import subprocess
import sys
from unittest.mock import Mock

import pytest

from sprint.trial.trial import supervise_services
from sprint.local_process import stop_services


@pytest.mark.asyncio
async def test_failed_service_stops_only_its_agent():
    stopped = asyncio.Event()

    async def agent():
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    async def failure():
        raise ValueError("feedback delivery exhausted")

    service = asyncio.create_task(failure())
    affected = asyncio.create_task(supervise_services(agent(), [service]))
    independent = asyncio.create_task(
        supervise_services(asyncio.sleep(0.01, result=7), [])
    )
    with pytest.raises(RuntimeError, match="service terminated") as error:
        await asyncio.wait_for(affected, 1)
    assert isinstance(error.value.__cause__, ValueError)
    assert stopped.is_set()
    assert await independent == 7


@pytest.mark.asyncio
async def test_agent_completion_leaves_service_for_owner_cleanup():
    service = asyncio.create_task(asyncio.sleep(60))
    try:
        assert await supervise_services(asyncio.sleep(0, result=3), [service]) == 3
        assert not service.done()
    finally:
        service.cancel()
        await asyncio.gather(service, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancellation_drains_agent_without_cancelling_service():
    service = asyncio.create_task(asyncio.sleep(60))
    agent = asyncio.create_task(asyncio.sleep(60))
    monitored = asyncio.create_task(supervise_services(agent, [service]))
    await asyncio.sleep(0)
    monitored.cancel()
    with pytest.raises(asyncio.CancelledError):
        await monitored
    assert agent.cancelled()
    assert not service.done()
    service.cancel()
    await asyncio.gather(service, return_exceptions=True)


@pytest.mark.asyncio
async def test_subprocess_shutdown_flushes_audit_once(tmp_path):
    marker = tmp_path / "audit-finished"
    program = f"""import signal
import sys
from pathlib import Path

def stop(*_):
    Path({str(marker)!r}).write_text("done")
    sys.exit(0)

signal.signal(signal.SIGTERM, stop)
print("ready", flush=True)
signal.pause()
"""
    process = subprocess.Popen([sys.executable, "-c", program], stdout=subprocess.PIPE)
    handles = [process]
    records = tmp_path / "shutdown.jsonl"
    try:
        if process.stdout is None:
            raise RuntimeError("service output pipe missing")
        assert process.stdout.readline() == b"ready\n"
        await stop_services(handles, records=records)
        await stop_services(handles, records=records)
        assert marker.read_text() == "done"
        values = [json.loads(line) for line in records.read_text().splitlines()]
        assert len(values) == 1 and values[0]["return_code"] == 0
        assert values[0]["forced"] is False
        assert not handles
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        if process.stdout is not None:
            process.stdout.close()


@pytest.mark.asyncio
async def test_shutdown_records_forced_stop_and_continues_after_failed_handle(tmp_path):
    bad = Mock(pid=123, returncode=None)
    bad.poll.return_value = None
    bad.terminate.side_effect = OSError("cannot signal service")
    timed_out = Mock(pid=124, returncode=-9)
    timed_out.poll.return_value = None
    timed_out.wait.side_effect = [subprocess.TimeoutExpired("service", 45), -9]
    handles = [bad, timed_out]
    records = tmp_path / "shutdown.jsonl"
    with pytest.raises(ExceptionGroup, match="shutdown failed"):
        await stop_services(handles, records=records)
    assert handles == [bad]
    timed_out.kill.assert_called_once()
    values = [json.loads(line) for line in records.read_text().splitlines()]
    assert values[0]["error_type"] == "OSError"
    assert values[1]["forced"] is True and values[1]["return_code"] == -9
