"""Neutral skill-listing schema.

A skill is a Hermes-side thing (a `SKILL.md` under that crew member's
`~/.hermes/skills/`) — this is the engine-agnostic shape the core speaks;
only an engine's adapter knows how it actually gets the data (Hermes: a live
`GET /v1/skills` on that crew member's own gateway).
"""
from __future__ import annotations

from pydantic import BaseModel, field_validator


class SkillInfo(BaseModel):
    name: str
    description: str = ""
    category: str | None = None

    @field_validator("description", mode="before")
    @classmethod
    def _no_description_is_empty(cls, value):
        # A SKILL.md without a description comes over as null; that's still a skill.
        return "" if value is None else value
