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


class MessageDoc(BaseModel):
    id: str = Field(default_factory=new_id)
    session_id: str
    role: str  # user | assistant (later: tool, system)
    content: str
    created_at: datetime = Field(default_factory=utcnow)
    attachments: list[AttachmentMeta] = []
