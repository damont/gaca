"""Request/response shapes for first-run setup (routers/setup.py)."""
from __future__ import annotations

from pydantic import BaseModel, EmailStr, Field


class SetupStatus(BaseModel):
    """What a client needs to know before it can sign in — public, no auth."""

    name: str = "gaca-api"
    version: str
    # False only until the one owner exists. The app shows "Set up this server" while False
    # and a normal sign-in screen afterwards.
    claimed: bool
    # Which sign-in methods this server offers, in preference order.
    sign_in: list[str]
    # Public by design (it is in every Google sign-in page); None when Google is off.
    google_client_id: str | None = None


class ClaimRequest(BaseModel):
    code: str = Field(..., min_length=1, max_length=64, description="The one-time setup code shown on the server")
    name: str = Field(..., min_length=1, max_length=100)
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=256)


class GoogleClaimRequest(BaseModel):
    code: str = Field(..., min_length=1, max_length=64)
    id_token: str
