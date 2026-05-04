"""Bus → MCP session fan-out for server-initiated push notifications.

The MCP Python SDK doesn't expose a public registry of active sessions, so we
monkey-patch `mcp.server.lowlevel.server.ServerSession` with a subclass that
self-registers on `__aenter__`. The patch is idempotent and applied lazily
the first time the broadcaster starts. Single point of fragility against SDK
upgrades — audit on `mcp` bumps.

The broadcast loop runs at process scope (mounted into the daemon's parent
Starlette lifespan) and subscribes to the in-process EventBus. On every
event it sends:

  * `notifications/message` (logging) with severity mapped from event name
  * `notifications/resources/updated` for each subscribable taskmux URI

Slow / failed sessions are skipped — the bus's drop policy already absorbs
back-pressure, but a per-session exception shouldn't take the loop down.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Iterable
from typing import Any

from ..event_bus import EventBus, getEventBus
from .scope import currentPin

logger = logging.getLogger(__name__)


_activeSessions: list[Any] = []
_trackerInstalled: bool = False


def _installSessionTracker() -> list[Any]:
    """Patch the SDK's `ServerSession` with a self-registering subclass.

    Idempotent. Returns the module-level list of currently-active sessions.
    """
    global _trackerInstalled
    from mcp.server.session import ServerSession

    if _trackerInstalled:
        return _activeSessions

    base_aenter = ServerSession.__aenter__
    base_aexit = ServerSession.__aexit__

    async def _tracked_aenter(self: Any) -> Any:
        result = await base_aenter(self)
        # Capture the request's pin (set by PinExtractionMiddleware) onto
        # the session so the broadcast loop can filter notifications later.
        # ServerSession is created during the initialize request, so this
        # contextvar reflects that request's URL `?session=` value.
        # ContextVar reads inherit through anyio task-group `start`, which
        # is how StreamableHTTPSessionManager spawns this run_server task.
        self._taskmux_pin = currentPin.get()
        _activeSessions.append(self)
        return result

    async def _tracked_aexit(self: Any, *args: Any) -> Any:
        with contextlib.suppress(ValueError):
            _activeSessions.remove(self)
        return await base_aexit(self, *args)

    ServerSession.__aenter__ = _tracked_aenter  # type: ignore[method-assign]
    ServerSession.__aexit__ = _tracked_aexit  # type: ignore[method-assign]
    ServerSession._taskmux_tracked = True  # type: ignore[attr-defined]
    _trackerInstalled = True
    return _activeSessions


# Default URIs to ping with `notifications/resources/updated` on every event.
# Clients that subscribed to any of these via `resources/subscribe` get a
# nudge to re-read; clients that didn't subscribe just ignore the event.
DEFAULT_PING_URIS: tuple[str, ...] = (
    "taskmux://status",
    "taskmux://events/recent",
    "taskmux://projects",
)


_LEVEL_FOR_EVENT: dict[str, str] = {
    "task_started": "info",
    "task_stopped": "info",
    "task_restarted": "info",
    "task_killed": "warning",
    "task_exited": "warning",  # promoted to error when exit_code != 0
    "health_check_failed": "error",
    "auto_restart": "warning",
    "max_restarts_reached": "error",
    "session_started": "info",
    "session_stopped": "info",
    "config_reloaded": "info",
}


def levelForEvent(event: dict[str, Any]) -> str:
    """Map event name → MCP log level. Unknown events default to `info`."""
    name = str(event.get("event", ""))
    level = _LEVEL_FOR_EVENT.get(name, "info")
    if name == "task_exited" and event.get("exit_code", 0) not in (0, None):
        level = "error"
    return level


def _shouldDeliver(session: Any, event: dict[str, Any]) -> bool:
    """Pin-based filter — pinned sessions only receive events for their
    own session; unpinned (admin) sessions get everything.
    """
    pin = getattr(session, "_taskmux_pin", None)
    if pin is None:
        return True
    return event.get("session") == pin


async def _safeSend(
    session: Any,
    level: str,
    event: dict[str, Any],
    pingUris: Iterable[str],
) -> None:
    if not _shouldDeliver(session, event):
        return
    try:
        await session.send_log_message(level=level, data=event, logger="taskmux")
    except Exception as e:  # noqa: BLE001
        logger.debug("send_log_message failed: %s", e)
        return
    for uri in pingUris:
        try:
            await session.send_resource_updated(uri)
        except Exception as e:  # noqa: BLE001
            logger.debug("send_resource_updated(%s) failed: %s", uri, e)


async def broadcastLoop(
    bus: EventBus | None = None,
    *,
    pingUris: Iterable[str] = DEFAULT_PING_URIS,
    eventFilter: Iterable[str] | None = None,
) -> None:
    """Subscribe to the bus and fan out every event to every active session.

    `eventFilter`: when None or empty, every event flows through. Otherwise
    only events whose `event` name appears in the set are pushed. Drops are
    silent — the JSONL log still records every event.

    Cancellation-safe: if the lifespan task is cancelled we exit cleanly
    via the subscribe() context manager.
    """
    sessions = _installSessionTracker()
    bus = bus or getEventBus()
    pingUrisList = list(pingUris)
    allowed: set[str] | None = set(eventFilter) if eventFilter else None
    async with bus.subscribe() as q:
        while True:
            event = await q.get()
            if allowed is not None and event.get("event") not in allowed:
                continue
            level = levelForEvent(event)
            for session in list(sessions):
                await _safeSend(session, level, event, pingUrisList)


@contextlib.asynccontextmanager
async def broadcasterLifespan(
    bus: EventBus | None = None,
    *,
    eventFilter: Iterable[str] | None = None,
):
    """Lifespan helper — start the broadcaster on enter, cancel on exit.

    Compose with FastMCP's session-manager lifespan so the daemon's parent
    Starlette gets one combined lifespan covering both.
    """
    task = asyncio.create_task(broadcastLoop(bus, eventFilter=eventFilter))
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
