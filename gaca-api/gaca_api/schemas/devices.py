"""Request/response shapes for paired devices (routers/devices.py)."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class PairRequest(BaseModel):
    code: str = Field(..., min_length=1, max_length=64, description="The one-time code `gaca-api pair` printed")
    name: str = Field("Phone", min_length=1, max_length=64, description="What to call this device, e.g. 'Pixel 7'")


class PairResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    device_id: str


class DeviceInfo(BaseModel):
    id: str
    name: str
    created_at: datetime
    last_used_at: datetime | None
    current: bool = Field(False, description="True for the device making this request")
