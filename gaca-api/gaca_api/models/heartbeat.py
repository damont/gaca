"""The heartbeat's settings: how often it may speak, and when it must stay quiet.

One per user, stored inside the user's own document (`UserDoc.heartbeat`), so there is no table and
no migration: a user saved before this existed simply reads back the defaults.

The app writes these; the agent only reads them. That asymmetry is the point: a model must never be
able to raise its own daily alert cap. It is enforced by the token scope (see utils/tokens.py), not
by trusting the agent.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator


def valid_timezone(value: str | None) -> str | None:
    """An IANA zone name ("America/Chicago") or None; anything else is a 422, not a stored typo."""
    if value is None or not value.strip():
        return None
    try:
        ZoneInfo(value.strip())
    except Exception as error:  # noqa: BLE001 - ZoneInfo raises several types for a bad name
        raise ValueError(f"{value!r} is not a time zone name (try America/Chicago)") from error
    return value.strip()


class HeartbeatSettings(BaseModel):
    # The master switch. Off, the heartbeat still wakes on schedule but its first step stops it
    # (one tiny model call), so for a real saving remove the Hermes job instead.
    enabled: bool = True
    # "Pause for a while": the heartbeat stays quiet until this moment, then carries on by itself.
    paused_until: datetime | None = None
    # The user's zone, for quiet hours. None = the Hermes machine's own zone.
    timezone: str | None = None
    # Quiet hours: no runs from `quiet_start_hour` until `quiet_end_hour` (0-23, local time).
    # Equal values mean "no quiet hours".
    quiet_start_hour: int = Field(default=22, ge=0, le=23)
    quiet_end_hour: int = Field(default=7, ge=0, le=23)
    # The most alerts it may send in a day, and the least gap between two. `urgent` ones skip both.
    max_alerts_per_day: int = Field(default=3, ge=0, le=20)
    min_minutes_between_alerts: int = Field(default=60, ge=0, le=1440)
    # A cost ceiling: each run is a model call.
    max_runs_per_day: int = Field(default=30, ge=0, le=96)

    @field_validator("timezone")
    @classmethod
    def _zone(cls, value: str | None) -> str | None:
        return valid_timezone(value)
