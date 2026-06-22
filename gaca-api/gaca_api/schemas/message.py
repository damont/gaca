"""Request/response models for messages — the client-facing shapes.

These never carry the `_id` alias; the client always sees `id`. They're built
from the DB models in the router (`from_attributes=True` lets us validate
straight off a MessageDoc).
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class MessageCreate(BaseModel):
    """Request body for POST /sessions/{id}/messages."""
    content: str
    role: str = "user"


class MessageOut(BaseModel):
    """A message as returned to the client."""
    model_config = ConfigDict(from_attributes=True)

    id: str
    session_id: str
    role: str
    content: str
    created_at: datetime


class MessageAccepted(BaseModel):
    """202 body: the message was stored; nothing consumes it yet."""
    turn_id: str
    status: str = "accepted"
