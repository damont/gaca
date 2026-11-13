"""Database models for the cloud link (db/cloud.py). Not what the API returns: see
schemas/cloud.py for that. The token fields here hold ciphertext only."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class CloudLink(BaseModel):
    user_id: str
    cloud_url: str
    status: str  # pending | linked | invalid
    scope: str
    server_name: str
    account_id: str | None = None
    handle: str | None = None
    server_id: str | None = None
    token_enc: str | None = None  # Fernet ciphertext of the gsrv_ token
    linked_at: datetime | None = None
    pending_device_enc: str | None = None  # Fernet ciphertext of the device code
    pending_expires_at: datetime | None = None
    pending_interval: int | None = None
    pending_next_poll: datetime | None = None
    created_at: datetime
    agent_access: bool = False  # the owner let Hermes tokens carry the `cloud` scope


class CloudPointer(BaseModel):
    """A cloud gacabit known to this server. `id` and `cloud_id` are the same value (a gacabit
    keeps its id); both columns exist so the pointer says plainly what it points at."""
    id: str
    user_id: str
    cloud_id: str
    account_id: str
    role: str
    shared_by: str | None = None
    title: str
    member_count: int = 1
    position: int | None = None
    last_seen: datetime
    meta: dict[str, Any] = Field(default_factory=dict)  # the last Gacabit JSON seen
