"""In-process event bus — publish/subscribe by session, swap-ready for Redis.

The bus carries events from the read_run task to whatever SSE stream is currently
open for a session. It is DELIBERATELY shaped like Redis pub/sub so moving to
Redis later is a drop-in with no caller changes:

  * publish(channel, message) / subscribe(channel) -> async iterator, keyed by an
    opaque channel string (we use the session_id).
  * messages are already-serialized strings (JSON) — exactly what Redis carries,
    not live Python objects, so nothing downstream depends on object identity.
  * EPHEMERAL: if no subscriber is attached when a message is published, it is
    dropped from the live path. Durability is SQLite's job — events are persisted
    before publish, and a reconnecting client replays from SQLite by
    Last-Event-ID, then attaches here for live.
  * connect()/aclose() lifecycle hooks (no-ops here) so the Redis version can
    open/close its client without the app wiring changing.

To move to Redis: implement the same four methods against redis.asyncio and
point `event_bus` at it. Publishers and subscribers stay exactly as they are.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Protocol


class EventBus(Protocol):
    """The neutral bus contract — InProcessEventBus and a future RedisEventBus
    both satisfy it, so the app depends only on this."""

    async def connect(self) -> None: ...
    async def aclose(self) -> None: ...
    async def publish(self, channel: str, message: str) -> None: ...
    def subscribe(self, channel: str) -> AsyncIterator[str]: ...


class InProcessEventBus:
    """A single-process EventBus backed by asyncio queues, one set per channel."""

    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[str]]] = {}

    async def connect(self) -> None:
        return None

    async def aclose(self) -> None:
        self._subscribers.clear()

    async def publish(self, channel: str, message: str) -> None:
        # Snapshot the set: a subscriber may unsubscribe while we iterate.
        for queue in list(self._subscribers.get(channel, ())):
            queue.put_nowait(message)

    def subscribe(self, channel: str) -> AsyncIterator[str]:
        # Register the queue EAGERLY (at call time, not first iteration) so a
        # caller can subscribe *before* running its catch-up replay and not miss
        # events published in between — this closes the subscribe/replay race.
        queue: asyncio.Queue[str] = asyncio.Queue()
        self._subscribers.setdefault(channel, set()).add(queue)

        async def _drain() -> AsyncIterator[str]:
            try:
                while True:
                    yield await queue.get()
            finally:
                subs = self._subscribers.get(channel)
                if subs is not None:
                    subs.discard(queue)
                    if not subs:
                        self._subscribers.pop(channel, None)

        return _drain()


# The process-wide bus. Swapping to Redis = point this at a RedisEventBus().
event_bus: EventBus = InProcessEventBus()
