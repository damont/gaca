"""HTTP layer for authentication.

Mirrors `api/routes/auth.py` in calendarapp and toolshed: the same endpoints,
the same status codes, the same inline HTTPExceptions. Register returns a token
rather than a user, so signing up logs you straight in.
"""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, status

from ..models.user import UserDoc
from ..schemas.auth import (
    AgentTokenRequest,
    AgentTokenResponse,
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
