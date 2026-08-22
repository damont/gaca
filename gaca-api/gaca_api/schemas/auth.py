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


class AgentConfigUpdate(BaseModel):
    """Body for PUT /auth/agent-config — where the user points GACA at their
    own agent. Both required together; there's no partial update."""

    agent_base_url: str = Field(..., min_length=1, max_length=500)
    agent_api_key: str = Field(..., min_length=1, max_length=500)


class AgentConfigOut(BaseModel):
    """Never echoes the key back — same rule as a password. The base URL isn't
    secret, so it's fine to read back and show in a settings screen."""

    agent_base_url: str | None = None
    agent_configured: bool
