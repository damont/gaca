"""Request/response models for push token registration."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class PushTokenRegister(BaseModel):
    """Body for POST /api/push/register — sent whenever a client gets a fresh
    device token: the Android app's FCM token (first launch, or
    FirebaseMessagingService.onNewToken), or the iPhone app's APNs token
    (platform "ios", from didRegisterForRemoteNotificationsWithDeviceToken)."""

    token: str = Field(..., min_length=1, max_length=4096)
    platform: str = Field(default="android", max_length=20)
    # iOS only: which APNs host the token belongs to — "sandbox" for a debug
    # build run from Xcode, "production" for TestFlight/App Store. Omitted (or
    # null) on an ios token means production. Ignored for other platforms.
    environment: Literal["sandbox", "production"] | None = None


class PushTokenUnregister(BaseModel):
    """Body for POST /api/push/unregister — sent on sign-out, so a stale
    token doesn't keep receiving another account's alerts on a shared device."""

    token: str = Field(..., min_length=1, max_length=4096)
