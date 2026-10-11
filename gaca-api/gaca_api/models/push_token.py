"""Database model for a registered device push token."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from .base import new_id, utcnow


class PushTokenDoc(BaseModel):
    id: str = Field(default_factory=new_id)
    user_id: str
    # An FCM registration token. Opaque, device-specific, and rotates — the
    # Android app re-registers on every onNewToken() callback, and a token
    # FCM reports as unregistered gets deleted here (services/push.py).
    token: str
    platform: str = "android"
    # APNs environment ("sandbox" | "production") for an ios token; None means
    # production (services/apns.py). Always None for FCM tokens.
    environment: str | None = None
    # The paired device (credential) that registered this token. Revoking that device deletes its
    # token, so an unpaired phone stops getting alerts. None for tokens registered before this existed;
    # the app re-registers on launch, which links them.
    device_id: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
