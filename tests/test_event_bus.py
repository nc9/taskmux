"""Tests for taskmux.mcp.event_bus."""

from __future__ import annotations

import asyncio

import pytest

from taskmux import event_bus
from taskmux.event_bus import EventBus, getEventBus, publishEvent, publishEventSync


@pytest.fixture(autouse=True)
def resetSingleton() -> None:
    """Singleton bus survives the process; reset so cross-test pollution can't happen."""
    event_bus._BUS = None


def testPublishDeliversToAllSubscribers() -> None:
    async def run() -> None:
        bus = EventBus()
        async with bus.subscribe() as a, bus.subscribe() as b:
            await bus.publish({"event": "task_started", "task": "api"})
            await bus.publish({"event": "task_exited", "task": "api", "exit_code": 1})

            assert (await asyncio.wait_for(a.get(), 0.5))["event"] == "task_started"
            assert (await asyncio.wait_for(a.get(), 0.5))["event"] == "task_exited"
            assert (await asyncio.wait_for(b.get(), 0.5))["event"] == "task_started"
            assert (await asyncio.wait_for(b.get(), 0.5))["event"] == "task_exited"

    asyncio.run(run())


def testPublishWithNoSubscribersIsNoop() -> None:
    async def run() -> None:
        bus = EventBus()
        await bus.publish({"event": "x"})

    asyncio.run(run())


def testUnsubscribeOnContextExit() -> None:
    async def run() -> None:
        bus = EventBus()
        async with bus.subscribe():
            assert bus.subscriber_count == 1
        assert bus.subscriber_count == 0

    asyncio.run(run())


def testSlowConsumerDropsOldest() -> None:
    async def run() -> None:
        bus = EventBus()
        async with bus.subscribe(maxsize=2) as q:
            await bus.publish({"n": 1})
            await bus.publish({"n": 2})
            await bus.publish({"n": 3})

            first = await asyncio.wait_for(q.get(), 0.5)
            second = await asyncio.wait_for(q.get(), 0.5)
            assert (first["n"], second["n"]) == (2, 3)

    asyncio.run(run())


def testGetEventBusReturnsSingleton() -> None:
    assert getEventBus() is getEventBus()


def testPublishEventHelperHitsSingleton() -> None:
    async def run() -> None:
        bus = getEventBus()
        async with bus.subscribe() as q:
            await publishEvent({"event": "ping"})
            got = await asyncio.wait_for(q.get(), 0.5)
            assert got["event"] == "ping"

    asyncio.run(run())


def testPublishEventSyncSchedulesOnRunningLoop() -> None:
    async def run() -> None:
        bus = getEventBus()
        async with bus.subscribe() as q:
            publishEventSync({"event": "sync_ping"})
            got = await asyncio.wait_for(q.get(), 0.5)
            assert got["event"] == "sync_ping"

    asyncio.run(run())


def testPublishEventSyncWithoutLoopIsNoop() -> None:
    publishEventSync({"event": "lonely"})


def testRecordEventPublishesToBus(tmp_path) -> None:
    """End-to-end: recordEvent must fan out through the bus when a loop runs."""
    from unittest.mock import patch

    from taskmux.events import recordEvent

    async def run() -> None:
        bus = getEventBus()
        async with bus.subscribe() as q:
            events_file = tmp_path / "events.jsonl"
            with (
                patch("taskmux.events.EVENTS_DIR", tmp_path),
                patch("taskmux.events.EVENTS_FILE", events_file),
            ):
                recordEvent("task_exited", session="proj", task="api", exit_code=137)

            got = await asyncio.wait_for(q.get(), 0.5)
            assert got["event"] == "task_exited"
            assert got["session"] == "proj"
            assert got["task"] == "api"
            assert got["exit_code"] == 137

    asyncio.run(run())
