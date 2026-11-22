"""Database model for a reminder: the stored shape.

A reminder is something a person asked to be nudged about, or accepted when an agent offered it. With a
`due_at` it fires as an ordinary alert at that time. Without one it is a "later" note: it never pings,
it is carried as short-term memory until it expires (see services/reminders.py and docs/reminders.md).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, Field, field_validator

from .base import new_id, utcnow

# pending -> fired (an alert was made) -> done; pending or fired -> cancelled; a note that runs out is expired.
STATES = ("pending", "fired", "done", "cancelled", "expired")
OPEN_STATES = ("pending", "fired")
SOURCE_KINDS = ("chat", "alert", "agent")
# How long an unfired "later" note is kept when nobody says.
NOTE_LIFETIME = timedelta(days=30)
# A reminder fired more than this long after it was due is marked late.
LATE_AFTER = timedelta(minutes=30)


def to_utc_seconds(value: datetime) -> datetime:
    """UTC, whole seconds. Stored times must compare correctly as text, so they all share one format."""
    if value.tzinfo is None:
        raise ValueError("time needs a UTC offset (for example 2026-11-23T15:00:00Z)")
    return value.astimezone(timezone.utc).replace(microsecond=0)


class ReminderSource(BaseModel):
    """Where a reminder came from: advisory, only for telling them apart and linking back."""

    kind: str = "agent"
    crew: str | None = Field(default=None, max_length=100)
    session_id: str | None = Field(default=None, max_length=64)
    alert_id: str | None = Field(default=None, max_length=64)


class ReminderDoc(BaseModel):
    id: str = Field(default_factory=new_id)
    user_id: str
    text: str
    due_at: datetime | None = None
    # True when the person named the time ("at 10"), False when an agent picked it ("tomorrow", "later").
    # An explicit time fires even in quiet hours or a pause; a vague one waits them out.
    explicit_time: bool = False
    state: str = "pending"
    source: ReminderSource = Field(default_factory=ReminderSource)
    # Notes only: when an unfired "later" note stops being worth keeping.
    expires_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    fired_at: datetime | None = None
    late: bool = False
    alert_id: str | None = None

    @field_validator("due_at", "expires_at", "fired_at", mode="after")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return to_utc_seconds(value) if value else None

    @property
    def is_note(self) -> bool:
        return self.due_at is None
