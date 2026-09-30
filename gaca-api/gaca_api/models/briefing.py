"""Database models for the daily briefing — the stored shape."""
from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field

from .base import utcnow


class BriefingItemDoc(BaseModel):
    title: str
    body: str | None = None
    # Free text ("3:30 PM", "All day") — whoever posts it knows the local
    # timezone and formatting; the API doesn't reinterpret it.
    time: str | None = None
    location: str | None = None
    link: str | None = None
    priority: str = "medium"  # high | medium | low


class BriefingSectionDoc(BaseModel):
    user_id: str
    date: date
    key: str  # slug: "calendar", "weather", "news", "sports", ...
    title: str
    kind: str = "general"  # display hint: calendar | weather | news | sports | tasks | general
    headline: str | None = None
    items: list[BriefingItemDoc] = []
    display_order: int = 0
    updated_at: datetime = Field(default_factory=utcnow)


class BriefingDoc(BaseModel):
    """The overall, per-day part of a briefing (its sections are stored apart)."""
    user_id: str
    date: date
    summary: str | None = None
    updated_at: datetime = Field(default_factory=utcnow)
