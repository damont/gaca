"""Request/response models for auth — the client-facing shapes.

Names and fields match `api/schemas/dto/auth.py` in calendarapp and toolshed so
a client written against one app reads the same on this one.
"""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator

from ..models.user import normalize_crew_path


class UserResponse(BaseModel):
    id: str
    name: str
    email: str


class MessageResponse(BaseModel):
    message: str


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


class CrewAccess(BaseModel):
    """What gaca-api needs to set up a crew member on the box directly,
    instead of the caller doing it by hand — a materially bigger grant than
    a crew member's own `api_key`, since this reaches the box itself, not
    one scoped Hermes gateway. Supply it once, on any call that creates or
    discovers crew members (`CrewMemberCreate.access`,
    `CrewDiscover.access`) — gaca-api remembers it for next time, so it
    only has to be given the first time. How gaca-api actually uses this
    (SSH, today) is not part of this contract — only that it's an access
    credential for the box, shaped like one."""

    host: str = Field(..., min_length=1, max_length=300)
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(..., min_length=1, max_length=100)
    private_key: str = Field(..., min_length=1)


def _valid_crew_path(value: str | None) -> str | None:
    return None if value is None else normalize_crew_path(value)


_HEX_COLOR = Field(
    default=None,
    pattern=r"^#[0-9A-Fa-f]{6}$",
    description="Hex color, e.g. \"#4E7093\". Omit to auto-assign from the palette.",
)


class CrewMemberCreate(BaseModel):
    """Body for POST /crew — register a new named crew member.

    Give `port` + `api_key` to register one you already set up by hand.
    Omit both to have gaca-api create a brand new one on the box directly
    instead — it picks the port and generates the key. That needs access to
    the box: give it via `access` the first time (gaca-api remembers it for
    every call after), or omit `access` too if you already have on a prior
    call — `400` (`CrewNotConfigured`) if neither this call nor a prior one
    ever supplied it."""

    name: str = Field(..., min_length=1, max_length=50)
    port: int | None = Field(default=None, ge=1, le=65535)
    api_key: str | None = Field(default=None, min_length=1, max_length=500)
    # Where under host:port the engine serves this member, e.g. "/p/toolshed"; omit when
    # it has a port of its own.
    path: str | None = Field(default=None, max_length=200)
    color: str | None = _HEX_COLOR
    access: CrewAccess | None = None

    _check_path = field_validator("path")(_valid_crew_path)

    @model_validator(mode="after")
    def _port_and_key_together(self) -> "CrewMemberCreate":
        if (self.port is None) != (self.api_key is None):
            raise ValueError(
                "port and api_key must be given together, or both omitted"
            )
        return self


class CrewDiscover(BaseModel):
    """Body for POST /crew/discover — entirely optional. Give `access` the
    first time (or if you haven't via a create call either); omit it once
    gaca-api already has it from a prior call."""

    access: CrewAccess | None = None


class CrewMemberUpdate(BaseModel):
    """Body for PUT /crew/{name}. Port and key are required together, same
    no-partial-update rule the single-agent form used to have; `color` alone
    can change without retyping the key."""

    port: int = Field(..., ge=1, le=65535)
    api_key: str = Field(..., min_length=1, max_length=500)
    # Omit to keep the current path; "" to clear it; "/p/name" to set it.
    path: str | None = Field(default=None, max_length=200)
    color: str | None = _HEX_COLOR

    _check_path = field_validator("path")(_valid_crew_path)


class CrewMemberOut(BaseModel):
    """Never echoes the key back — same rule as a password."""

    name: str
    port: int
    # Where under host:port this member is served ("" = its own port, "/p/name" = shared gateway).
    path: str = ""
    color: str
    is_default: bool


class CrewOut(BaseModel):
    """The full picture: shared host, every configured crew member, and
    whether creating one can skip `port`/`api_key` (i.e. whether gaca-api
    already has access to the box) — so a client can decide whether its
    "add crew member" form needs to ask for `access` before deciding a
    plain name is enough. Every mutating crew endpoint returns this, so the
    client can just replace its state wholesale after any change."""

    host: str | None
    crew: list[CrewMemberOut]
    can_create: bool
