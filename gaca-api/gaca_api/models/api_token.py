"""An API token's public record. The secret itself is never stored — only its hash, in the table."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from .base import new_id, utcnow


class ApiTokenDoc(BaseModel):
    id: str = Field(default_factory=new_id)
    user_id: str
    name: str
    scopes: list[str]
    created_at: datetime = Field(default_factory=utcnow)
    last_used_at: datetime | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
