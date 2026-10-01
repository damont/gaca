"""A crew member's profile: GACA's own identity for it plus what its engine reports.

No FastAPI, no HTTP. GACA stores a crew member's name, color and address; the
engine (through its adapter) says how it is actually set up. This joins the two.
"""
from __future__ import annotations

from ..models.user import UserDoc
from ..schemas.crew_profile import CrewProfile
from . import auth as auth_service
from .exceptions import NotFound


async def get(user: UserDoc, name: str) -> CrewProfile:
    member = auth_service.find_crew_member(user, name)
    if member is None:
        raise NotFound("crew member", name)
    info = await auth_service.backend_for_crew(user, member).describe_profile()
    return CrewProfile(
        **info.model_dump(),
        name=member.name,
        color=member.color,
        is_default=member.name == user.default_crew,
    )
