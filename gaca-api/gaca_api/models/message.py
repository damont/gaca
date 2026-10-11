"""Database model for a message — the stored shape (system of record)."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from .base import new_id, utcnow


class AttachmentMeta(BaseModel):
    """What a message remembers about a photo it carries (the bytes are elsewhere)."""
    id: str
    content_type: str
    size: int


class ToolUsage(BaseModel):
    """One tool's tally within a run."""
    name: str
    count: int
    errors: int = 0


class RunActivity(BaseModel):
    """What the agent did during the run that produced an assistant message.

    Computed once, when the run completes (services/runs.py), from that run's
    persisted tool events — the events are the raw log, this is the summary a
    client shows under the reply ("Ran terminal 3× · 41s"). Stored on the
    message rather than recomputed per read so the transcript doesn't depend on
    the event log being kept forever.
    """
    tools: list[ToolUsage]  # in order of first use
    duration_seconds: float
    label: str


class MessageDoc(BaseModel):
    id: str = Field(default_factory=new_id)
    session_id: str
    role: str  # user | assistant (later: tool, system)
    content: str
    created_at: datetime = Field(default_factory=utcnow)
    attachments: list[AttachmentMeta] = []
    # Assistant messages only, and only when the run used a tool. Messages
    # stored before this existed have no key and read back as None.
    activity: RunActivity | None = None
