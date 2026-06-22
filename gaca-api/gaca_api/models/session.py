"""Database model for a session — how it is stored in Mongo.

Mongo's primary key is always `_id`; we map our `id` onto it via the alias, so a
document round-trips cleanly (dump by_alias=True to store, Model(**doc) to load).
This alias detail is intentionally confined to the DB layer.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from .base import new_id, utcnow


class SessionDoc(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(default_factory=new_id, alias="_id")
    created_at: datetime = Field(default_factory=utcnow)
    title: str | None = None
    status: str = "idle"  # idle | active (the agent flips this later)
