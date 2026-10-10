"""First-run setup: is this server claimed yet, and claim it. Public by design.

An app pointed at a URL calls GET /api/setup to learn what to show: a "Set up this
server" screen (account + one-time setup code) while unclaimed, an ordinary sign-in
screen afterwards. The claim endpoints stop working the moment an owner exists.
See services/setup.py for the rules around the code.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from ..config import settings
from ..schemas.auth import TokenResponse
from ..schemas.setup import ClaimRequest, GoogleClaimRequest, SetupStatus
from ..services import setup as setup_service
from ..utils.auth import create_access_token
from ..utils.google import GoogleAuthError, verify_google_identity

router = APIRouter(prefix="/setup", tags=["setup"])

_STATUS = {
    "claimed": status.HTTP_409_CONFLICT,
    "no_code": status.HTTP_403_FORBIDDEN,
    "expired": status.HTTP_403_FORBIDDEN,
    "burned": status.HTTP_403_FORBIDDEN,
    "wrong_code": status.HTTP_401_UNAUTHORIZED,
}


def _refuse(error: setup_service.SetupRefused) -> HTTPException:
    return HTTPException(status_code=_STATUS.get(error.reason, 400), detail=error.detail)


@router.get("", response_model=SetupStatus)
async def setup_status():
    from ..main import app  # version lives on the app; imported late to avoid a cycle

    return SetupStatus(
        version=app.version,
        claimed=await setup_service.is_claimed(),
        sign_in=["password", "google"] if settings.google_client_id else ["password"],
        google_client_id=settings.google_client_id or None,
    )


@router.post("/claim", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def claim(data: ClaimRequest):
    """Become this server's owner with an email and password. 409 once it has an owner."""
    try:
        user = await setup_service.claim_with_password(data.code, data.name, data.email, data.password)
    except setup_service.SetupRefused as error:
        raise _refuse(error)
    return TokenResponse(access_token=create_access_token(subject=user.id))


@router.post("/claim/google", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def claim_with_google(data: GoogleClaimRequest):
    """Become this server's owner with a Google account (when Google sign-in is configured)."""
    try:
        identity = verify_google_identity(data.id_token)
    except GoogleAuthError as error:
        raise HTTPException(status_code=error.status_code, detail=error.detail)
    try:
        user = await setup_service.claim_with_google(
            data.code, google_sub=identity["sub"], email=identity["email"], name=identity["name"]
        )
    except setup_service.SetupRefused as error:
        raise _refuse(error)
    return TokenResponse(access_token=create_access_token(subject=user.id))
