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


class AgentHostUpdate(BaseModel):
    """Body for PUT /auth/agent-host — the scheme+host shared by every one of
    this user's agents, e.g. "http://david-claw". No port: each agent below
    supplies its own."""

    host: str = Field(..., min_length=1, max_length=300)


class AgentCreate(BaseModel):
    """Body for POST /auth/agents — register a new named agent."""

    name: str = Field(..., min_length=1, max_length=50)
    port: int = Field(..., ge=1, le=65535)
    api_key: str = Field(..., min_length=1, max_length=500)


class AgentUpdate(BaseModel):
    """Body for PUT /auth/agents/{name}. Both fields required together, same
    no-partial-update rule the single-agent form used to have."""

    port: int = Field(..., ge=1, le=65535)
    api_key: str = Field(..., min_length=1, max_length=500)


class AgentOut(BaseModel):
    """Never echoes the key back — same rule as a password."""

    name: str
    port: int
    is_default: bool


class AgentsOut(BaseModel):
    """The full picture: shared host plus every configured agent. Every
    mutating agent endpoint returns this, so the client can just replace its
    state wholesale after any change."""

    host: str | None
    agents: list[AgentOut]
