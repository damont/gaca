"""HTTP layer for authentication.

Mirrors `api/routes/auth.py` in calendarapp and toolshed: the same endpoints,
the same status codes, the same inline HTTPExceptions. Register returns a token
rather than a user, so signing up logs you straight in.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from ..models.user import UserDoc
from ..schemas.auth import (
    GoogleLoginRequest,
    RegisterRequest,
    TokenResponse,
    UserLogin,
    UserResponse,
)
from ..services import auth as auth_service
from ..utils.auth import create_access_token, get_current_user, verify_password
from ..utils.google import GoogleAuthError, verify_google_identity

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", include_in_schema=False)
async def register(request: RegisterRequest):
    """Closed: a GACA server has exactly one owner, created through POST /api/setup/claim.

    Kept (rather than removed) so an older app build gets a clear message, not a 404."""
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Registration is closed. This server has a single owner; set it up with the setup code.",
    )


@router.post("/login", response_model=TokenResponse)
async def login(data: UserLogin):
    """Login with email and password."""
    user = await auth_service.find_by_email(data.email)
    if (
        not user
        or not user.hashed_password
        or not verify_password(data.password, user.hashed_password)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = create_access_token(subject=user.id)
    return TokenResponse(access_token=token)


@router.post("/google", response_model=TokenResponse)
async def google_login(data: GoogleLoginRequest):
    """Sign in with a Google ID token — as the server's existing owner only.

    Never creates an account (that is the setup claim's job, with the setup code): a Google
    identity that is not the owner's is refused, so enabling Google sign-in does not open
    registration to every Google user.
    """
    try:
        identity = verify_google_identity(data.id_token)
    except GoogleAuthError as error:
        raise HTTPException(status_code=error.status_code, detail=error.detail)

    user = await auth_service.resolve_google_user(google_sub=identity["sub"], email=identity["email"])
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="That Google account is not this server's owner.",
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User account is disabled",
        )
    return TokenResponse(access_token=create_access_token(subject=user.id))


@router.get("/me", response_model=UserResponse)
async def me(user: UserDoc = Depends(get_current_user)):
    """Get current user info."""
    return UserResponse(id=user.id, name=user.display_name, email=user.email)
