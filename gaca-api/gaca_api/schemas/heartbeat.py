"""Request/response shapes for the heartbeat settings."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from ..models.heartbeat import HeartbeatSettings, valid_timezone

# What a client reads: the settings exactly as stored.
HeartbeatSettingsOut = HeartbeatSettings


class HeartbeatSettingsUpdate(BaseModel):
    """PUT body: send only what you are changing. A field you leave out is left alone; `paused_until`
    and `timezone` can be set to null to clear them."""

    enabled: bool | None = None
    paused_until: datetime | None = None
    timezone: str | None = None
    quiet_start_hour: int | None = Field(default=None, ge=0, le=23)
    quiet_end_hour: int | None = Field(default=None, ge=0, le=23)
    max_alerts_per_day: int | None = Field(default=None, ge=0, le=20)
    min_minutes_between_alerts: int | None = Field(default=None, ge=0, le=1440)
    max_runs_per_day: int | None = Field(default=None, ge=0, le=96)

    @field_validator("timezone")
    @classmethod
    def _zone(cls, value: str | None) -> str | None:
        return valid_timezone(value)
