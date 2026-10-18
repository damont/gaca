"""Neutral skill-listing schema.

A skill is a Hermes-side thing (a `SKILL.md` under that crew member's
`~/.hermes/skills/`) — this is the engine-agnostic shape the core speaks;
only an engine's adapter knows how it actually gets the data (Hermes: a live
`GET /v1/skills` on that crew member's own gateway).
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, field_validator


class SkillInfo(BaseModel):
    name: str
    description: str = ""
    category: str | None = None
    # How often the engine has used it, when the engine keeps count (0 when it doesn't or
    # never has), and when it last did. Lets a client rank skills by what actually gets used.
    use_count: int = 0
    last_used_at: datetime | None = None

    @field_validator("description", mode="before")
    @classmethod
    def _no_description_is_empty(cls, value):
        # A SKILL.md without a description comes over as null; that's still a skill.
        return "" if value is None else value


class SkillUsage(BaseModel):
    """One skill's usage as an engine counts it; merged into SkillInfo by the service."""
    use_count: int = 0
    last_used_at: datetime | None = None
