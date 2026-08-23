"""HTTP layer for push token registration."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ..models.user import UserDoc
from ..schemas.push import PushTokenRegister, PushTokenUnregister
from ..services import push as push_service
from ..utils.auth import get_current_user

router = APIRouter(prefix="/push", tags=["push"])


@router.post("/register", status_code=204)
async def register(
    body: PushTokenRegister,
    user: UserDoc = Depends(get_current_user),
) -> None:
    """Register (or move) a device's FCM token to this account.

    Called on app launch and whenever FirebaseMessagingService.onNewToken()
    fires — tokens rotate, so this is a "keep me current," not a one-time
    call. Idempotent: registering the same token again is a no-op.
    """
    await push_service.register_token(user.id, body.token, body.platform)


@router.post("/unregister", status_code=204)
async def unregister(
    body: PushTokenUnregister,
    user: UserDoc = Depends(get_current_user),
) -> None:
    """Drop a device's token — called on sign-out so a shared/reset device
    doesn't keep receiving this account's alerts after someone else signs in."""
    await push_service.unregister_token(user.id, body.token)
