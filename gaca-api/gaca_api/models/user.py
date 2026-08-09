"""Database model for a user — the stored shape (system of record).

Note what is NOT here: the plaintext password. It never reaches this model, is
never written to SQLite, and is never logged. Registration hashes it on the way
in and the hash is all we keep.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from .base import new_id, utcnow


class UserDoc(BaseModel):
    id: str = Field(default_factory=new_id)
    # Stored lowercase — the service normalizes before both writes and lookups,
    # so "David@x.com" and "david@x.com" are the same account. The UNIQUE index
    # on this column is case-sensitive, so without normalizing you'd get two.
    email: str
    display_name: str
    # Argon2 hash. Carries its own salt and parameters inside the string, so
    # there is no separate salt column to keep in sync.
    hashed_password: str
    is_active: bool = True
    created_at: datetime = Field(default_factory=utcnow)
