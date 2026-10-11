"""Who am I. (There is no sign-in: phones pair with the server, see routers/devices.py.)"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ..models.user import UserDoc
from ..schemas.auth import UserResponse
from ..utils.auth import get_current_user

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/me", response_model=UserResponse)
async def me(user: UserDoc = Depends(get_current_user)):
    """The owner this credential belongs to."""
    return UserResponse(id=user.id, name=user.display_name, email=user.email)
