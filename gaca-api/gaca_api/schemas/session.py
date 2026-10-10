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
    """A session plus its messages — GET /sessions/{id}.

    Also says whether a run is in flight, and where to pick up its event stream:
    open GET /sessions/{id}/events with Last-Event-ID: `resume_seq` and the
    stream replays exactly the in-flight run's progress (nothing older — those
    runs are already in `messages`), then continues live. So a client never has
    to guess which replayed events are stale.
    """
    messages: list[MessageOut] = []
    # A run for this session is in flight in the API right now. Cleared when it
    # completes or fails — and by a server restart, which ends the run too.
    running: bool = False
    run_started_at: datetime | None = None
    # Not running: the session's latest event seq (0 if none) — resume from
    # there and only new events arrive. Running: just before the run's first
    # event, so its progress so far is replayed.
    resume_seq: int = 0


class SessionList(BaseModel):
    """GET /sessions — the Chat tab's list, newest first."""
    sessions: list[SessionOut] = []
