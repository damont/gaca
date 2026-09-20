"""Database model for a session — the stored shape (system of record)."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from .base import new_id, utcnow


class SessionDoc(BaseModel):
    id: str = Field(default_factory=new_id)
    # Owner. Predates multi-user auth — this app's original chat feature had
    # no user scoping at all until it was retrofitted here alongside requiring
    # auth on every session route.
    user_id: str
    created_at: datetime = Field(default_factory=utcnow)
    title: str | None = None
    status: str = "idle"  # idle | active (the agent flips this later)
    # Which of the user's crew members this session talks to, resolved once
    # at creation and fixed for its lifetime (no mid-thread swap). Optional
    # only so a session row from before crew existed still loads — a session
    # actually created today always gets one, via routers/sessions.py or
    # alerts.delegate().
    crew: str | None = None
    # The alert that started this session via delegation, if any — the
    # chat-side half of the bidirectional link; the alert holds the forward
    # pointer (AlertDoc.session_id).
    alert_id: str | None = None
