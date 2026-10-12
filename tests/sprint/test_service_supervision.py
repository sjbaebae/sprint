"""Per-trial service failures must not strand or cancel independent agents."""

import asyncio

import pytest

from sprint.trial.trial import supervise_services


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
