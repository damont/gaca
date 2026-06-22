"""Session business logic. No FastAPI, no HTTP — just orchestration."""
from __future__ import annotations

from ..db import messages as messages_db
from ..db import sessions as sessions_db
from ..models.message import MessageDoc
from ..models.session import SessionDoc
from .exceptions import NotFound


async def create(title: str | None) -> SessionDoc:
    session = SessionDoc(title=title)
    await sessions_db.insert(session)
    return session


async def ensure_exists(session_id: str) -> None:
    """Raise NotFound if the session doesn't exist. Used to 404 before streaming."""
    if not await sessions_db.exists(session_id):
        raise NotFound("session", session_id)


async def get_with_messages(session_id: str) -> tuple[SessionDoc, list[MessageDoc]]:
    session = await sessions_db.find(session_id)
    if session is None:
        raise NotFound("session", session_id)
    return session, await messages_db.find_by_session(session_id)
