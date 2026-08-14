"""HTTP layer for authentication.

Mirrors `api/routes/auth.py` in calendarapp and toolshed: the same endpoints,
the same status codes, the same inline HTTPExceptions. Register returns a token
rather than a user, so signing up logs you straight in.
"""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token

from ..config import settings
from ..models.user import UserDoc
from ..schemas.auth import (
    AgentTokenRequest,
    AgentTokenResponse,
    GoogleLoginRequest,
    RegisterRequest,
    TokenResponse,
    UserLogin,
    UserResponse,
)
from ..services import auth as auth_service
from ..utils.auth import create_access_token, get_current_user, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED
)
async def register(request: RegisterRequest):
    """Register a new user account."""
    existing = await auth_service.find_by_email(request.email)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email already registered",
        )

    user = await auth_service.create_user(
        name=request.name, email=request.email, password=request.password
    )

    token = create_access_token(subject=user.id)
    return TokenResponse(access_token=token)


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
    """Sign in with a Google ID token.

    The token arrives from the browser, so it is verified here and never
    trusted as-is: `verify_oauth2_token` checks Google's signature, that the
    audience is *our* client id, and that it hasn't expired. Without the
    audience check, a token minted for any other Google app would be accepted.
    """
    if not settings.google_client_id:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Google login not configured",
        )

    try:
        idinfo = google_id_token.verify_oauth2_token(
            data.id_token, google_requests.Request(), settings.google_client_id
        )
    except ValueError:
        # Bad signature, wrong audience, or expired — all indistinguishable to
        # the caller on purpose. Caught narrowly so a genuine bug still raises.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Google token",
        )

    sub = idinfo.get("sub")
    email = idinfo.get("email")
    email_verified = idinfo.get("email_verified", False)
    name = idinfo.get("name") or (email.split("@")[0] if email else None)

    # An unverified Google email is refused: without this, someone could sign up
    # to Google with an address they don't own and use it to claim the matching
    # account here through the linking path below.
    if not sub or not email or not email_verified:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Google token missing required claims",
        )

    user = await auth_service.upsert_google_user(
        google_sub=sub, email=email, name=name
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


@router.post("/agent-token", response_model=AgentTokenResponse)
async def agent_token(
    data: AgentTokenRequest,
    user: UserDoc = Depends(get_current_user),
):
    """Create a long-lived JWT for AI agent access for the authenticated user.

    Protected by the same dependency as every other authenticated route: the
    caller already holds a session token, so there's no password re-prompt. The
    minted token is an ordinary bearer JWT naming the same user — it just lives
    longer, and get_current_user accepts it transparently.
    """
    token = create_access_token(
        subject=user.id,
        expires_delta=timedelta(days=data.expires_in_days),
    )
    return AgentTokenResponse(
        access_token=token,
        expires_in_days=data.expires_in_days,
    )
