"""Request/response models for sessions — the client-facing shapes."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from .message import MessageOut


class SessionCreate(BaseModel):
    """Request body for POST /sessions."""
    title: str | None = None


class SessionOut(BaseModel):
    """A session as returned to the client (always `id`, never `_id`)."""
    model_config = ConfigDict(from_attributes=True)

    id: str
    created_at: datetime
    title: str | None = None
    status: str


class SessionDetail(SessionOut):
    """A session plus its messages — GET /sessions/{id}."""
    messages: list[MessageOut] = []
