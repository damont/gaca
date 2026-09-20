"""Request/response models for auth — the client-facing shapes.

Names and fields match `api/schemas/dto/auth.py` in calendarapp and toolshed so
a client written against one app reads the same on this one.
"""
from __future__ import annotations

from pydantic import BaseModel, EmailStr, Field


class RegisterRequest(BaseModel):
    name: str = Field(..., description="Display name")
    email: EmailStr
    password: str


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class GoogleLoginRequest(BaseModel):
    """The ID token the Google button hands back, verified server-side."""

    id_token: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserResponse(BaseModel):
    id: str
    name: str
    email: str


class MessageResponse(BaseModel):
    message: str


class AgentTokenRequest(BaseModel):
    expires_in_days: int = Field(default=30, ge=1, le=365)


class AgentTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in_days: int


class AgentConnect(BaseModel):
    """Body for POST /agent/connect — first-time setup. Connects the account
    to its one Hermes agent and creates `name` as the first crew member from
    `port`/`api_key`, so there's something to chat with immediately."""

    host: str = Field(..., min_length=1, max_length=300)
    port: int = Field(..., ge=1, le=65535)
    api_key: str = Field(..., min_length=1, max_length=500)
    name: str = Field(default="default", min_length=1, max_length=50)


class AgentHostUpdate(BaseModel):
    """Body for PUT /agent — move the connected agent (and every crew member
    under it) to a new host, e.g. "http://david-claw". No port: each crew
    member supplies its own."""

    host: str = Field(..., min_length=1, max_length=300)


class AgentStatus(BaseModel):
    """GET /agent — is there an agent connected yet, and where."""

    connected: bool
    host: str | None


class ProvisioningAccess(BaseModel):
    """Body for PUT /agent/provisioning — lets gaca-api set up new crew
    members on your Hermes box directly (choosing a port, creating the
    profile) instead of you creating one by hand and re-entering the
    result. A materially bigger grant than a crew member's `api_key`: this
    reaches the box itself, not one scoped Hermes gateway. How gaca-api
    actually uses this (SSH, today) is not part of this contract — only
    that it's an access credential for the box, shaped like one."""

    host: str = Field(..., min_length=1, max_length=300)
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(..., min_length=1, max_length=100)
    private_key: str = Field(..., min_length=1)


class ProvisioningStatus(BaseModel):
    """GET /agent/provisioning — never echoes the credential, same rule as
    a password."""

    enabled: bool
    host: str | None
    username: str | None


class ProvisioningTestResult(BaseModel):
    """POST /agent/provisioning/test — did the stored access actually work."""

    ok: bool
    message: str


_HEX_COLOR = Field(
    default=None,
    pattern=r"^#[0-9A-Fa-f]{6}$",
    description="Hex color, e.g. \"#4E7093\". Omit to auto-assign from the palette.",
)


class CrewMemberCreate(BaseModel):
    """Body for POST /crew — register a new named crew member."""

    name: str = Field(..., min_length=1, max_length=50)
    port: int = Field(..., ge=1, le=65535)
    api_key: str = Field(..., min_length=1, max_length=500)
    color: str | None = _HEX_COLOR


class CrewMemberUpdate(BaseModel):
    """Body for PUT /crew/{name}. Port and key are required together, same
    no-partial-update rule the single-agent form used to have; `color` alone
    can change without retyping the key."""

    port: int = Field(..., ge=1, le=65535)
    api_key: str = Field(..., min_length=1, max_length=500)
    color: str | None = _HEX_COLOR


class CrewMemberOut(BaseModel):
    """Never echoes the key back — same rule as a password."""

    name: str
    port: int
    color: str
    is_default: bool


class CrewOut(BaseModel):
    """The full picture: shared host plus every configured crew member. Every
    mutating crew endpoint returns this, so the client can just replace its
    state wholesale after any change."""

    host: str | None
    crew: list[CrewMemberOut]
