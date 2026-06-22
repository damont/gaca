"""What flows on a session's SSE stream.

Phase-1 stand-in: replay the session's stored messages, then POLL Mongo for new
ones. Polling is deliberately temporary — the next slice replaces it with Redis
pub/sub (the agent posts an event, the API publishes, every open stream gets it
instantly). The router/transport concerns (SSE framing, keep-alive pings,
disconnect) are handled by EventSourceResponse, not here.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime

from ..db import messages as messages_db
from ..models.message import MessageDoc
from ..schemas.message import MessageOut

POLL_INTERVAL = 1.0  # seconds — placeholder until Redis pub/sub lands


def _sse_event(message: MessageDoc) -> dict:
    """Shape one message into an SSE event dict (EventSourceResponse formats it).

    `event` becomes the SSE event type, `id` the Last-Event-ID, `data` the JSON
    payload the client renders.
    """
    return {
        "event": "message",
        "id": message.id,
        "data": MessageOut.model_validate(message).model_dump_json(),
    }


async def stream(session_id: str) -> AsyncIterator[dict]:
    after: datetime | None = None

    # 1) replay everything stored so far
    for message in await messages_db.find_by_session(session_id):
        after = message.created_at
        yield _sse_event(message)

    # 2) live: poll for anything newer (the temporary stand-in for pub/sub)
    while True:
        await asyncio.sleep(POLL_INTERVAL)
        for message in await messages_db.find_by_session(session_id, after=after):
            after = message.created_at
            yield _sse_event(message)
