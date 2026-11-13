"""The current-owner dependencies: who a bearer credential belongs to.

Two kinds of credential exist, both stored hashed and individually revocable:
  * a paired device (the app): `gaca_dev_…`, the owner's full access — see services/pairing.py;
  * an API token (a Hermes skill, a script): `gaca_pat_…`, only what its scopes allow — see utils/tokens.py.
There are no passwords and no sessions to sign.
"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer

from ..models.user import UserDoc
from ..services import tokens as token_service
from . import tokens as token_utils

# The "token URL" only feeds the interactive docs and the 401 challenge; pairing is the way in.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/devices/pair")


async def _resolve(request: Request | None, token: str, *, allow_api_token: bool) -> UserDoc:
    """The owner a bearer credential belongs to. A paired device gets full access; an API token
    only gets through where its scopes allow, and never where `allow_api_token` is False."""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    if token_utils.looks_like_device(token):
        # The reserved scope check is belt and braces: only pairing ever mints this prefix.
        found = await token_service.authenticate(token)
        if found is None or not token_service.is_device(found[1]):
            raise credentials_exception
        return found[0]

    if token_utils.looks_like_token(token):
        found = await token_service.authenticate(token)
        if found is None:
            raise credentials_exception
        user, record = found
        if token_service.is_device(record):
            raise credentials_exception
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

    raise credentials_exception


async def device_id_for(token: str) -> str | None:
    """The paired-device id behind a bearer credential, or None if it is not a (live) device."""
    if not token_utils.looks_like_device(token):
        return None
    found = await token_service.authenticate(token)
    return found[1].id if found and token_service.is_device(found[1]) else None


async def get_current_user(token: str = Depends(oauth2_scheme), request: Request = None) -> UserDoc:
    """Dependency: the owner — via a paired device, or an API token within its scopes."""
    return await _resolve(request, token, allow_api_token=True)


async def get_session_user(token: str = Depends(oauth2_scheme), request: Request = None) -> UserDoc:
    """Like get_current_user but for owner-only actions an API token must never perform
    (managing tokens and devices): a paired device is required."""
    return await _resolve(request, token, allow_api_token=False)


@dataclass
class Caller:
    """Who is calling. `is_agent`: software holding an API token (Hermes, a script) rather than the
    person's own paired app. `cloud`: may see and use cloud-homed gacabits: always for the app, for
    a token only if it carries the opt-in `cloud` scope."""
    user: UserDoc
    is_agent: bool
    cloud: bool = True


_optional_scheme = OAuth2PasswordBearer(tokenUrl="/api/devices/pair", auto_error=False)


async def get_caller(user: UserDoc = Depends(get_current_user), token: str | None = Depends(_optional_scheme)) -> Caller:
    """The owner plus what kind of credential reached us. Builds on get_current_user (so the same
    checks and the same test overrides apply)."""
    if not token or token_utils.looks_like_device(token):
        return Caller(user=user, is_agent=False, cloud=True)
    found = await token_service.authenticate(token)
    scopes = found[1].scopes if found else []
    return Caller(user=user, is_agent=True, cloud="cloud" in scopes)
