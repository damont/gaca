"""Request/response shapes for API tokens (routers/tokens.py)."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class TokenCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=60, description="What this token is for, e.g. 'hermes-toolshed'")
    scopes: list[Literal["alerts", "briefings", "artifacts", "heartbeat"]] = Field(..., min_length=1)
    expires_in_days: int | None = Field(default=None, ge=1, le=3650, description="Omit for a token that does not expire")


class TokenInfo(BaseModel):
    """A token as listed — never includes the secret."""

    id: str
    name: str
    scopes: list[str]
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None


class TokenCreated(TokenInfo):
    token: str = Field(..., description="The secret. Shown once; only its hash is stored.")
