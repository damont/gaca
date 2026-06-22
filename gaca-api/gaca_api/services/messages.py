"""Message business logic."""
from __future__ import annotations

from ..db import messages as messages_db
from ..db import sessions as sessions_db
from ..models.message import MessageDoc
from .exceptions import NotFound


async def add(session_id: str, role: str, content: str) -> MessageDoc:
    if not await sessions_db.exists(session_id):
        raise NotFound("session", session_id)
    message = MessageDoc(session_id=session_id, role=role, content=content)
    await messages_db.insert(message)
    return message
