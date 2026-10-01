"""Neutral crew-profile schema — GACA's own definition of "how a crew member is set up".

A crew member is a named agent profile. What gaca-api *stores* about one is small
(name, color, address, key); what a person wants to *see* is bigger: which model it
runs, how it can be reached, what it can do, and what it does on a schedule. This
file defines that picture in GACA's terms. Each engine's adapter translates its own
vocabulary into it (see agent/<engine>/translate_profile.py), so clients — and the
rest of the API — never learn how any particular engine names things.

Read-only by design: changing a profile is done by talking to it in chat.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class ProfileModel(BaseModel):
    """The model this profile runs on, as the engine reports it."""
    provider: str | None = None
    name: str | None = None


class ProfileChannel(BaseModel):
    """A way this profile can be reached or can reach out (chat apps, the API...)."""
    name: str
    state: Literal["connected", "disconnected", "error"] = "disconnected"
    needs_attention: bool = False


class SkillCategory(BaseModel):
    name: str
    count: int


class ProfileToolset(BaseModel):
    """A group of tools the profile is allowed to use (web, terminal, files...)."""
    name: str
    label: str
    description: str = ""


class ProfileJob(BaseModel):
    """Something the profile does on a schedule."""
    id: str
    name: str
    schedule: str  # plain English, e.g. "Every day at 7:00 AM"
    enabled: bool = True
    next_run: datetime | None = None
    last_run: datetime | None = None
    last_status: str | None = None  # "ok" | "error" | None if it hasn't run
    kind: Literal["agent", "script"] = "agent"  # whether a model runs each time


class ProfileInfo(BaseModel):
    """What an engine can tell us about one profile (no GACA-side identity yet)."""
    status: Literal["online", "offline"] = "offline"
    engine: str | None = None  # e.g. "Hermes 0.20.5"
    model: ProfileModel | None = None
    channels: list[ProfileChannel] = Field(default_factory=list)
    skill_count: int = 0
    skill_categories: list[SkillCategory] = Field(default_factory=list)
    toolsets: list[ProfileToolset] = Field(default_factory=list)
    jobs: list[ProfileJob] = Field(default_factory=list)
    # Pieces we couldn't read, in words ("scheduled jobs"), so the UI can say
    # "couldn't load X" instead of silently showing an empty list as if it were true.
    unavailable: list[str] = Field(default_factory=list)


class CrewProfile(ProfileInfo):
    """GET /crew/{name}/profile — GACA's identity for the member plus the engine's picture."""
    name: str
    color: str
    is_default: bool
