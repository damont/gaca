"""Request/response models for alerts — the client-facing shapes.

Note the asymmetry with models/alert.py: the DB model keeps `severity` and
`status` as plain strings, but the request schema pins them to Literals. Input
from an agent gets validated (a bogus severity is a 422, not a stored typo);
stored data stays permissive so an old row never fails to load.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..models.alert import SuggestedAction


class AlertCreate(BaseModel):
    """Request body for POST /api/alerts — what an agent posts."""

    kind: str = Field(..., min_length=1, max_length=100)
    title: str = Field(..., min_length=1, max_length=500)
    severity: Literal["info", "warn", "urgent"] = "info"
    body: str | None = None
    data: dict = Field(default_factory=dict)
    dedupe_key: str | None = Field(default=None, max_length=200)
    suggested_actions: list[SuggestedAction] = Field(default_factory=list)
    # Advisory label for who posted this. Not authenticated — ownership comes
    # from the token, this is only for telling posters apart in the UI.
    source: str = "unknown"


class AlertOut(BaseModel):
    """An alert as returned to the client."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    user_id: str
    created_at: datetime
    source: str
    kind: str
    severity: str
    title: str
    body: str | None = None
    data: dict = {}
    dedupe_key: str | None = None
    suggested_actions: list[SuggestedAction] = []

    status: str
    # What the client should display and filter on. Differs from `status` when a
    # snooze has elapsed — see services/alerts.effective_status.
    effective_status: str
    status_changed_at: datetime
    snooze_until: datetime | None = None
    session_id: str | None = None
    resolution_note: str | None = None
    parent_alert_id: str | None = None


class AlertList(BaseModel):
    alerts: list[AlertOut] = []
