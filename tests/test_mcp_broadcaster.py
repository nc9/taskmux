"""Tests for taskmux.mcp.broadcaster (bus → MCP fan-out)."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import pytest

from taskmux import event_bus as bus_module
from taskmux.event_bus import EventBus
from taskmux.mcp import broadcaster


@pytest.fixture(autouse=True)
def resetState() -> None:
    bus_module._BUS = None
    broadcaster._activeSessions.clear()


def testLevelMapping() -> None:
    assert broadcaster.levelForEvent({"event": "task_started"}) == "info"
    assert broadcaster.levelForEvent({"event": "task_killed"}) == "warning"
    assert broadcaster.levelForEvent({"event": "health_check_failed"}) == "error"
    assert broadcaster.levelForEvent({"event": "auto_restart"}) == "warning"
    assert broadcaster.levelForEvent({"event": "max_restarts_reached"}) == "error"
    assert broadcaster.levelForEvent({"event": "task_exited", "exit_code": 0}) == "warning"
    assert broadcaster.levelForEvent({"event": "task_exited", "exit_code": 1}) == "error"
    assert broadcaster.levelForEvent({"event": "task_exited"}) == "warning"
    assert broadcaster.levelForEvent({"event": "unknown_event_type"}) == "info"


class _FakeSession:
    def __init__(self) -> None:
        self.logs: list[dict[str, Any]] = []
        self.updated: list[str] = []

    async def send_log_message(self, *, level: str, data: Any, logger: str) -> None:
        self.logs.append({"level": level, "data": data, "logger": logger})

    async def send_resource_updated(self, uri: str) -> None:
        self.updated.append(uri)


def testBroadcastLoopFansOutToAllSessions() -> None:
    """One event → log + resource ping per session per uri."""

    async def run() -> None:
        bus = EventBus()
        s1, s2 = _FakeSession(), _FakeSession()
        broadcaster._activeSessions.extend([s1, s2])

        loop_task = asyncio.create_task(
            broadcaster.broadcastLoop(bus, pingUris=("taskmux://status",))
        )
        await asyncio.sleep(0)  # let it subscribe

        await bus.publish({"event": "task_exited", "task": "api", "exit_code": 1})

        # Spin briefly so the loop processes the event.
        for _ in range(20):
            if s1.logs and s2.logs:
                break
            await asyncio.sleep(0.01)

        loop_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await loop_task

        assert len(s1.logs) == 1
        assert s1.logs[0]["level"] == "error"
        assert s1.logs[0]["data"]["task"] == "api"
        assert s1.updated == ["taskmux://status"]

        assert len(s2.logs) == 1
        assert s2.updated == ["taskmux://status"]

    asyncio.run(run())


def testBroadcastLoopSurvivesPerSessionFailure() -> None:
    """A throwing session shouldn't take the loop down or block siblings."""

    class _BrokenSession:
        async def send_log_message(self, **_kwargs: Any) -> None:
            raise RuntimeError("connection dead")

        async def send_resource_updated(self, _uri: str) -> None:
            raise RuntimeError("connection dead")

    async def run() -> None:
        bus = EventBus()
        broken = _BrokenSession()
        good = _FakeSession()
        broadcaster._activeSessions.extend([broken, good])

        loop_task = asyncio.create_task(
            broadcaster.broadcastLoop(bus, pingUris=("taskmux://status",))
        )
        await asyncio.sleep(0)

        await bus.publish({"event": "task_started", "task": "api"})

        for _ in range(20):
            if good.logs:
                break
            await asyncio.sleep(0.01)

        loop_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await loop_task

        assert good.logs and good.logs[0]["data"]["task"] == "api"

    asyncio.run(run())


def testEventFilterDropsNonMatching() -> None:
    """Configured event filter limits which events get pushed; durable
    JSONL log already captures the rest, so dropping is safe.
    """

    async def run() -> None:
        bus = EventBus()
        s = _FakeSession()
        broadcaster._activeSessions.append(s)

        loop_task = asyncio.create_task(
            broadcaster.broadcastLoop(
                bus,
                pingUris=("taskmux://status",),
                eventFilter={"task_exited"},
            )
        )
        await asyncio.sleep(0)

        await bus.publish({"event": "task_started", "task": "api"})
        await bus.publish({"event": "task_exited", "task": "api", "exit_code": 1})

        for _ in range(20):
            if s.logs:
                break
            await asyncio.sleep(0.01)

        # Brief wait to make sure the dropped event would have arrived too if
        # the filter wasn't applied.
        await asyncio.sleep(0.05)

        loop_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await loop_task

        assert len(s.logs) == 1
        assert s.logs[0]["data"]["event"] == "task_exited"

    asyncio.run(run())


def testPinnedSessionOnlyGetsMatchingEvents() -> None:
    """A session with `_taskmux_pin = 'foo'` must only receive events
    whose `session` field matches; cross-project events are dropped.
    """

    async def run() -> None:
        bus = EventBus()
        pinned = _FakeSession()
        pinned._taskmux_pin = "demo"  # type: ignore[attr-defined]
        broadcaster._activeSessions.append(pinned)

        loop_task = asyncio.create_task(
            broadcaster.broadcastLoop(bus, pingUris=("taskmux://status",))
        )
        await asyncio.sleep(0)

        await bus.publish({"event": "task_started", "session": "demo", "task": "api"})
        await bus.publish({"event": "task_started", "session": "other", "task": "x"})
        await bus.publish({"event": "task_exited", "session": "demo", "exit_code": 1})

        for _ in range(20):
            if len(pinned.logs) >= 2:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)

        loop_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await loop_task

        assert len(pinned.logs) == 2
        assert all(log["data"]["session"] == "demo" for log in pinned.logs)

    asyncio.run(run())


def testUnpinnedSessionGetsEverything() -> None:
    """An admin (unpinned) session must receive events for every project."""

    async def run() -> None:
        bus = EventBus()
        admin = _FakeSession()
        # No `_taskmux_pin` attribute — getattr returns None → deliver-all.
        broadcaster._activeSessions.append(admin)

        loop_task = asyncio.create_task(
            broadcaster.broadcastLoop(bus, pingUris=("taskmux://status",))
        )
        await asyncio.sleep(0)

        await bus.publish({"event": "task_started", "session": "demo"})
        await bus.publish({"event": "task_started", "session": "other"})

        for _ in range(20):
            if len(admin.logs) >= 2:
                break
            await asyncio.sleep(0.01)

        loop_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await loop_task

        assert {log["data"]["session"] for log in admin.logs} == {"demo", "other"}

    asyncio.run(run())


def testMixedClientsRespectIndividualPins() -> None:
    """Two pinned clients + an admin client all see the right subsets."""

    async def run() -> None:
        bus = EventBus()
        a = _FakeSession()
        a._taskmux_pin = "alpha"  # type: ignore[attr-defined]
        b = _FakeSession()
        b._taskmux_pin = "beta"  # type: ignore[attr-defined]
        admin = _FakeSession()
        broadcaster._activeSessions.extend([a, b, admin])

        loop_task = asyncio.create_task(
            broadcaster.broadcastLoop(bus, pingUris=("taskmux://status",))
        )
        await asyncio.sleep(0)

        await bus.publish({"event": "task_started", "session": "alpha"})
        await bus.publish({"event": "task_started", "session": "beta"})
        await bus.publish({"event": "task_started", "session": "gamma"})

        for _ in range(20):
            if len(admin.logs) >= 3:
                break
            await asyncio.sleep(0.01)

        loop_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await loop_task

        assert {log["data"]["session"] for log in a.logs} == {"alpha"}
        assert {log["data"]["session"] for log in b.logs} == {"beta"}
        assert {log["data"]["session"] for log in admin.logs} == {"alpha", "beta", "gamma"}

    asyncio.run(run())


def testInstallSessionTrackerIsIdempotent() -> None:
    """Calling the installer twice must not stack subclasses or reset the list."""
    sessions1 = broadcaster._installSessionTracker()
    sessions2 = broadcaster._installSessionTracker()
    assert sessions1 is sessions2

    from mcp.server.session import ServerSession

    assert getattr(ServerSession, "_taskmux_tracked", False) is True
