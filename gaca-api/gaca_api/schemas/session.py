"""Request/response models for sessions — the client-facing shapes."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from .message import MessageOut


class SessionCreate(BaseModel):
    """Request body for POST /sessions.

    `crew` names which of the caller's configured crew members this chat
    talks to; omit it to use the account's default. Unlike a delegated
    alert's `crew`, there's no soft-matched middle tier here — a session
    started directly has nothing else to fall back through.
    """
    title: str | None = None
    crew: str | None = None


class SessionOut(BaseModel):
    """A session as returned to the client (always `id`, never `_id`)."""
    model_config = ConfigDict(from_attributes=True)

    id: str
    created_at: datetime
    title: str | None = None
    status: str
    crew: str | None = None
    # The alert this session was opened from, if any — lets the client show
    # a "from alert" pill and link straight back. None for a session started
    # directly from the Chat tab.
    alert_id: str | None = None
    # What to show in the Chat list instead of the crew member's name: a short
    # headline (the title if one was set, else derived from the opening
    # message) and a one-line preview of the latest message. Computed by the
    # API so every client labels a chat the same way; null while a session has
    # no messages yet.
    summary: str | None = None
    preview: str | None = None


class SessionDetail(SessionOut):
    """A session plus its messages — GET /sessions/{id}."""
    messages: list[MessageOut] = []


class SessionList(BaseModel):
    """GET /sessions — the Chat tab's list, newest first."""
    sessions: list[SessionOut] = []
