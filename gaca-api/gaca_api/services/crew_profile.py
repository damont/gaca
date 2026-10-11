"""A crew member's profile: GACA's own identity for it plus what its engine reports.

No FastAPI, no HTTP. GACA stores a crew member's name, color and address; the
engine (through its adapter) says how it is actually set up. This joins the two.
"""
from __future__ import annotations

import asyncio

from ..agent.base import AgentBackend, ProfileReadError
from ..models.user import CrewMember, UserDoc
from ..provisioning.select import provisioner_for
from ..schemas.crew_profile import CrewProfile, ProfilePurpose
from ..utils.purpose import build_purpose
from . import auth as auth_service
from .exceptions import NotFound


async def _purpose(user: UserDoc, member: CrewMember, backend: AgentBackend) -> ProfilePurpose | None:
    """What the crew member is for, or None if it can't be read (no direct access to the
    box configured, unreachable, timed out). Never raises: the rest of the profile is
    still worth showing."""
    provisioner = provisioner_for(user)
    if provisioner is None:
        return None
    try:
        info = await asyncio.wait_for(
            backend.describe_purpose(name=member.name, provisioner=provisioner), timeout=12.0
        )
    except (ProfileReadError, asyncio.TimeoutError):
        return None
    return build_purpose(info)


async def get(user: UserDoc, name: str) -> CrewProfile:
    member = auth_service.find_crew_member(user, name)
    if member is None:
        raise NotFound("crew member", name)
    backend = auth_service.backend_for_crew(user, member)
    # Independent reads, done together so the slowest one sets the pace, not their sum.
    info, purpose = await asyncio.gather(
        backend.describe_profile(), _purpose(user, member, backend)
    )
    if purpose is None:
        info.unavailable.append("purpose")
    return CrewProfile(
        **info.model_dump(),
        purpose=purpose,
        name=member.name,
        color=member.color,
        is_default=member.name == user.default_crew,
    )
