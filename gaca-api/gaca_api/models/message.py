"""Database model for a message — how it is stored in Mongo."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from .base import new_id, utcnow


class MessageDoc(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(default_factory=new_id, alias="_id")
    session_id: str
    role: str  # user | assistant (later: tool, system)
    content: str
    created_at: datetime = Field(default_factory=utcnow)
