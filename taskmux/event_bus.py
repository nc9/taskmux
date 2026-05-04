"""In-process pub/sub for taskmux lifecycle events.

Producers: every `recordEvent` call site in supervisor.py + daemon.py publishes
the same event payload here. Consumers: MCP sessions (one subscriber per
connected client) and any future broadcaster (WS health subscribers, etc.).

Slow-consumer policy: each subscriber owns a bounded queue. When a queue is
full the *oldest* entry is dropped to make room — slow consumers lose
history, fast consumers and other subscribers stay current. This matches the
"taskmux is best-effort observability" posture; durability lives in
`~/.taskmux/events.jsonl`.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

DEFAULT_QUEUE_SIZE = 256


class EventBus:
    """Async fan-out bus. Publish is non-blocking; subscribers consume queues."""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[dict]] = set()
        self._lock = asyncio.Lock()

    async def publish(self, event: dict) -> None:
        """Deliver `event` to every current subscriber.

        Snapshots the subscriber set under the lock so a concurrent
        subscribe/unsubscribe doesn't race with delivery. On a full queue
        drops the oldest entry to make room for the newest — better to lose
        stale history than block the producer.
        """
        async with self._lock:
            queues = list(self._subscribers)
        for q in queues:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
                with contextlib.suppress(asyncio.QueueFull):
                    q.put_nowait(event)

    @contextlib.asynccontextmanager
    async def subscribe(
        self, maxsize: int = DEFAULT_QUEUE_SIZE
    ) -> AsyncIterator[asyncio.Queue[dict]]:
        """Yield a queue that receives every event published while open."""
        q: asyncio.Queue[dict] = asyncio.Queue(maxsize=maxsize)
        async with self._lock:
            self._subscribers.add(q)
        try:
            yield q
        finally:
            async with self._lock:
                self._subscribers.discard(q)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


_BUS: EventBus | None = None


def getEventBus() -> EventBus:
    """Module-level singleton. Mirrors the `recordEvent` access pattern."""
    global _BUS
    if _BUS is None:
        _BUS = EventBus()
    return _BUS


async def publishEvent(event: dict) -> None:
    """Async helper — publishes to the singleton bus."""
    await getEventBus().publish(event)


def publishEventSync(event: dict) -> None:
    """Sync caller — schedules a publish on the running loop. Best-effort.

    Drops silently when no loop is running (e.g., a CLI invocation that
    never reaches the daemon). Lifecycle events still hit `events.jsonl`
    via `recordEvent`; only the live push surface is skipped.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(getEventBus().publish(event))
