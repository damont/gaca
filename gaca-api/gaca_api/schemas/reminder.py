"""Request and response shapes for reminders (see docs/reminders.md)."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from ..models.reminder import ReminderDoc, ReminderSource, to_utc_seconds


def _aware(value: datetime | None) -> datetime | None:
    return to_utc_seconds(value) if value is not None else None


class ReminderSourceIn(ReminderSource):
    kind: Literal["chat", "alert", "agent"] = "agent"


class ReminderCreate(BaseModel):
    """What an agent posts. The agent turns "tomorrow at 10" into a UTC instant (it knows the date and
    the person's time zone); the API never parses natural language."""

    text: str = Field(..., min_length=1, max_length=500)
    # A time with an offset ("2026-11-23T15:00:00Z"). Leave it out for a "later" note.
    due_at: datetime | None = None
    # True only when the person gave the time themselves. Defaults to false (an agent's own pick).
    explicit_time: bool | None = None
    source: ReminderSourceIn = Field(default_factory=ReminderSourceIn)
    # Notes only. Defaults to 30 days from now.
    expires_at: datetime | None = None

    @field_validator("text")
    @classmethod
    def _tidy(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("text is empty")
        return value

    @field_validator("due_at", "expires_at")
    @classmethod
    def _needs_offset(cls, value: datetime | None) -> datetime | None:
        return _aware(value)

    @model_validator(mode="after")
    def _consistent(self) -> "ReminderCreate":
        if self.due_at is None and self.explicit_time:
            raise ValueError("explicit_time needs a due_at")
        return self


class ReminderUpdate(BaseModel):
    """Change some of it; what you leave out stays. A new `due_at` sets a fired reminder going again."""

    text: str | None = Field(default=None, min_length=1, max_length=500)
    due_at: datetime | None = None
    explicit_time: bool | None = None
    # `cancelled`: drop it (the chat's Undo). `done`: it is dealt with.
    state: Literal["cancelled", "done"] | None = None

    @field_validator("text")
    @classmethod
    def _tidy(cls, value: str | None) -> str | None:
        return " ".join(value.split()) or None if value else value

    @field_validator("due_at")
    @classmethod
    def _needs_offset(cls, value: datetime | None) -> datetime | None:
        return _aware(value)


class ReminderPreset(BaseModel):
    """A one-tap reschedule the app renders as a button. Computed here from the person's time zone."""

    key: str
    label: str
    due_at: datetime


class ReminderOut(BaseModel):
    id: str
    text: str
    kind: Literal["timed", "note"]
    due_at: datetime | None
    explicit_time: bool
    state: str
    source: ReminderSource
    expires_at: datetime | None
    created_at: datetime
    fired_at: datetime | None
    late: bool
    alert_id: str | None
    # Reschedule choices for a reminder that is pending or has just fired. Empty for a note.
    presets: list[ReminderPreset] = Field(default_factory=list)
    # Set on a create that found the same reminder already pending and made nothing new.
    duplicate: bool = False

    @classmethod
    def of(cls, doc: ReminderDoc, presets: list[ReminderPreset] | None = None, duplicate: bool = False) -> "ReminderOut":
        return cls(
            **doc.model_dump(exclude={"user_id", "updated_at"}),
            kind="note" if doc.is_note else "timed",
            presets=presets or [],
            duplicate=duplicate,
        )


class ReminderList(BaseModel):
    reminders: list[ReminderOut]


class FireDueOut(BaseModel):
    """What one `fire-due` call did, for the heartbeat's log."""

    fired: list[ReminderOut]
    # Due but waiting: a vague time during quiet hours or a pause.
    held: int = 0
    # "Later" notes that ran out and were dropped.
    expired: int = 0
