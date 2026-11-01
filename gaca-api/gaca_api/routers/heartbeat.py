"""HTTP layer for the heartbeat's settings.

The app reads and changes them; the heartbeat skill only reads them. The agent's token is allowed
GET and nothing else (utils/tokens.py), so it cannot raise its own limits.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ..models.user import UserDoc
from ..schemas.heartbeat import HeartbeatSettingsOut, HeartbeatSettingsUpdate
from ..services import heartbeat as heartbeat_service
from ..utils.auth import get_current_user

router = APIRouter(prefix="/heartbeat", tags=["heartbeat"])


@router.get("/settings", response_model=HeartbeatSettingsOut)
async def get_settings(user: UserDoc = Depends(get_current_user)):
    """The heartbeat's quiet hours, daily limits and on/off switch (defaults until changed)."""
    return heartbeat_service.get(user)


@router.put("/settings", response_model=HeartbeatSettingsOut)
async def put_settings(body: HeartbeatSettingsUpdate, user: UserDoc = Depends(get_current_user)):
    """Change some of them; anything you leave out stays as it was. App only: an agent token is
    refused (403), on purpose."""
    return await heartbeat_service.update(user, body)
