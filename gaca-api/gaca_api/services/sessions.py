"""Session business logic. No FastAPI, no HTTP — just orchestration."""
from __future__ import annotations

from ..db import messages as messages_db
from ..db import sessions as sessions_db
from ..models.message import MessageDoc
from ..models.session import SessionDoc
from .exceptions import NotFound


async def create(user_id: str, title: str | None) -> SessionDoc:
    session = SessionDoc(user_id=user_id, title=title)
    await sessions_db.insert(session)
    return session


async def ensure_owned(user_id: str, session_id: str) -> SessionDoc:
    """The session, if it exists and belongs to this user — else NotFound.

    404, not 403, same reasoning as alerts: a 403 would confirm the id exists,
    which someone else's caller has no business learning.
    """
    session = await sessions_db.find(session_id)
    if session is None or session.user_id != user_id:
        raise NotFound("session", session_id)
    return session


async def get_with_messages(
    user_id: str, session_id: str
) -> tuple[SessionDoc, list[MessageDoc]]:
    session = await ensure_owned(user_id, session_id)
    return session, await messages_db.find_by_session(session_id)
