"""Request/response models for push token registration."""
from __future__ import annotations

from pydantic import BaseModel, Field


class PushTokenRegister(BaseModel):
    """Body for POST /api/push/register — sent whenever the Android app gets
    a fresh FCM token (first launch, or FirebaseMessagingService.onNewToken)."""

    token: str = Field(..., min_length=1, max_length=4096)
    platform: str = Field(default="android", max_length=20)


class PushTokenUnregister(BaseModel):
    """Body for POST /api/push/unregister — sent on sign-out, so a stale
    token doesn't keep receiving another account's alerts on a shared device."""

    token: str = Field(..., min_length=1, max_length=4096)
