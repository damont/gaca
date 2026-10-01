"""Database model for a chat attachment (a photo) — the stored metadata.

The bytes live in their own column (db/attachments.py); this is everything else.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from .base import new_id, utcnow


class AttachmentDoc(BaseModel):
    id: str = Field(default_factory=new_id)
    user_id: str
    session_id: str
    # None until a message claims it.
    message_id: str | None = None
    content_type: str  # sniffed from the bytes, never trusted from the client
    size: int
    created_at: datetime = Field(default_factory=utcnow)
