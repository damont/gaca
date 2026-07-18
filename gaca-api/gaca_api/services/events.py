"""What flows on a session's SSE stream: neutral GacaEvents.

On connect we replay any events the client missed — everything after its
Last-Event-ID — from the durable log, then attach to the in-process bus for live
events published by read_run. The bus subscription registers eagerly, so events
published while we're mid-replay are buffered rather than lost; we de-dup them by
seq.

Transport concerns (SSE framing, keep-alive pings, disconnect) belong to
EventSourceResponse, not here — we just yield {event, id, data} dicts.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from ..bus import event_bus
from ..db import events as events_db
from ..schemas.event import GacaEvent, GacaEventAdapter


def _sse_event(event: GacaEvent) -> dict:
    """Frame one GacaEvent for SSE: type -> event, seq -> id, model JSON -> data."""
    return {
        "event": event.type,
        "id": "" if event.seq is None else str(event.seq),
        "data": event.model_dump_json(),
    }


async def stream(session_id: str, last_event_id: int = 0) -> AsyncIterator[dict]:
    last = last_event_id
    # Subscribe first (eager registration) so the live queue is buffering before
    # we go do the replay — otherwise events published during replay are missed.
    subscription = event_bus.subscribe(session_id)
    try:
        # 1) replay everything after the client's Last-Event-ID
        for event in await events_db.find_by_session(session_id, after_seq=last):
            last = event.seq
            yield _sse_event(event)

        # 2) live — de-dup anything the replay already covered
        async for raw in subscription:
            event = GacaEventAdapter.validate_json(raw)
            if event.seq is not None and event.seq <= last:
                continue
            if event.seq is not None:
                last = event.seq
            yield _sse_event(event)
    finally:
        await subscription.aclose()
