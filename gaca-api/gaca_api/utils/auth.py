"""Password hashing, JWT minting/validation, and the current-user dependency.

Mirrors `api/utils/auth.py` in calendarapp and toolshed so the auth layer looks
the same across the apps. The only differences are the ones the storage layer
forces: users come from `db/users.py` (aiosqlite) instead of a Beanie document,
and ids are hex uuids, so there's no PydanticObjectId to parse.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Optional

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer

from ..config import settings
from ..db import users as users_db
from ..models.base import utcnow
from ..models.user import UserDoc
from ..services import tokens as token_service
from . import tokens as token_utils

ph = PasswordHasher()
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")


def hash_password(password: str) -> str:
    """Hash a password using Argon2."""
    return ph.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against an Argon2 hash."""
    try:
        ph.verify(hashed_password, plain_password)
        return True
    except VerifyMismatchError:
        return False


def create_access_token(subject: str, expires_delta: Optional[timedelta] = None) -> str:
    """Create a JWT access token naming `subject` (a user id).

    A JWT is signed, not encrypted — anyone holding it can read the payload, so
    it carries only the subject and expiry. `expires_delta` overrides the default
    session lifetime.
    """
    now = utcnow()
    if expires_delta:
        expire = now + expires_delta
    else:
        expire = now + timedelta(minutes=settings.jwt_expire_minutes)
    payload = {"sub": subject, "exp": expire}
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> Optional[str]:
    """Return the subject (user id) if the token is valid, else None.

    `algorithms=` is pinned deliberately: letting the library read the algorithm
    from the token's own header lets an attacker present `"alg": "none"` and have
    it accepted unsigned.
    """
    try:
        payload = jwt.decode(
            token, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
        )
        return payload.get("sub")
    except jwt.PyJWTError:
        return None


async def _resolve(request: Request | None, token: str, *, allow_api_token: bool) -> UserDoc:
    """The user a bearer credential belongs to. Two kinds exist: a sign-in session (JWT) and an
    API token (utils/tokens.py) for software acting for the owner. An API token only gets
    through where its scopes allow, and never where `allow_api_token` is False."""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    if token_utils.looks_like_token(token):
        found = await token_service.authenticate(token)
        if found is None:
            raise credentials_exception
        user, record = found
        # No request to check the scopes against (a direct call, not through FastAPI) -> deny.
        if (
            not allow_api_token
            or request is None
            or not token_utils.allowed(record.scopes, request.method, request.url.path)
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This API token is not allowed to call that endpoint.",
            )
        return user

    user_id = decode_token(token)
    if user_id is None:
        raise credentials_exception

    user = await users_db.find(user_id)
    if user is None or not user.is_active:
        raise credentials_exception

    return user


async def get_current_user(token: str = Depends(oauth2_scheme), request: Request = None) -> UserDoc:
    """Dependency: the signed-in owner — via a session, or an API token within its scopes."""
    return await _resolve(request, token, allow_api_token=True)


async def get_session_user(token: str = Depends(oauth2_scheme), request: Request = None) -> UserDoc:
    """Like get_current_user but for owner-only actions an API token must never perform
    (managing tokens): a real sign-in session is required."""
    return await _resolve(request, token, allow_api_token=False)
