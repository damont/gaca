"""Database model for a session — the stored shape (system of record)."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from .base import new_id, utcnow


class SessionDoc(BaseModel):
    id: str = Field(default_factory=new_id)
    created_at: datetime = Field(default_factory=utcnow)
    title: str | None = None
    status: str = "idle"  # idle | active (the agent flips this later)
