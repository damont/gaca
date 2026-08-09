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
