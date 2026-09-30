"""Request/response models for the daily briefing — the client-facing shapes.

A briefing is one day's digest, assembled from independent sections. Each source
(calendar, weather, news, sports...) PUTs its own section, replacing whatever it
posted before for that day, so a cron job can re-run safely and one failing
source never blocks the others. Clients only render this; nothing here needs
interpreting.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator

SECTION_KEY_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,39}$"
SectionKey = Annotated[str, Field(pattern=SECTION_KEY_PATTERN)]

# What the section is, as a display hint (icon/layout). Anything else is "general".
SectionKind = Literal["general", "calendar", "weather", "news", "sports", "tasks"]
Priority = Literal["high", "medium", "low"]


class BriefingItem(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    body: str | None = Field(default=None, max_length=2000)
    time: str | None = Field(default=None, max_length=60)
    location: str | None = Field(default=None, max_length=200)
    link: str | None = Field(default=None, max_length=1000)
    priority: Priority = "medium"

    @field_validator("link")
    @classmethod
    def _http_links_only(cls, v: str | None) -> str | None:
        # The app opens these on tap; refuse anything but a web link.
        if v is not None and not re.match(r"^https?://", v, re.IGNORECASE):
            raise ValueError("link must start with http:// or https://")
        return v


class SectionPut(BaseModel):
    """Body for PUT /briefings/{date}/sections/{key} — replaces that section."""
    title: str = Field(min_length=1, max_length=100)
    kind: SectionKind = "general"
    headline: str | None = Field(default=None, max_length=300)
    items: list[BriefingItem] = Field(default_factory=list, max_length=50)
    display_order: int = 0


class SectionOut(BaseModel):
    key: str
    title: str
    kind: str
    headline: str | None = None
    items: list[BriefingItem] = []
    display_order: int = 0
    updated_at: datetime


class BriefingPut(BaseModel):
    """Body for PUT /briefings/{date} — the overall summary line(s)."""
    summary: str | None = Field(default=None, max_length=2000)


class BriefingOut(BaseModel):
    date: date
    summary: str | None = None
    sections: list[SectionOut] = []
    # Most recent write to anything in this briefing; null when it's empty.
    updated_at: datetime | None = None


class BriefingDates(BaseModel):
    """GET /briefings/dates — days that have content, newest first."""
    dates: list[date] = []
