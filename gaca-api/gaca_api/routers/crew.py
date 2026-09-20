"""HTTP layer for a user's crew — named Hermes profiles under their one
connected agent.

Routes are thin: validate the request schema, call a service, map the
returned DB model to a response schema. No business logic, no DB access here.
Split out of routers/auth.py once this grew past CRUD/default into also
carrying skills — a crew member is its own resource, not an auth concern.
Connecting the agent itself (and moving its host) lives in routers/agent.py —
a different question with a different lifecycle (once, vs. any time).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, status

from ..models.user import UserDoc
from ..schemas.auth import CrewMemberCreate, CrewMemberOut, CrewMemberUpdate, CrewOut
from ..schemas.skill import SkillInfo
from ..services import auth as auth_service
from ..utils.auth import get_current_user

router = APIRouter(prefix="/crew", tags=["crew"])


def crew_out(user: UserDoc) -> CrewOut:
    """The full crew picture for a user — shared with routers/agent.py, whose
    connect/host endpoints return this same shape so a client can just
    replace its state wholesale after either kind of call."""
    return CrewOut(
        host=user.agent_host,
        crew=[
            CrewMemberOut(
                name=c.name,
                port=c.port,
                color=c.color,
                is_default=c.name == user.default_crew,
            )
            for c in user.crew
        ],
    )


@router.get("", response_model=CrewOut)
async def list_crew(user: UserDoc = Depends(get_current_user)):
    """This user's shared host and every crew member configured under it.

    Never returns a key — same rule as a password.
    """
    return crew_out(user)


@router.post("", status_code=status.HTTP_201_CREATED, response_model=CrewOut)
async def add_crew_member(
    data: CrewMemberCreate,
    user: UserDoc = Depends(get_current_user),
):
    """Register another named crew member on the already-connected agent.
    Its color is auto-assigned from the palette unless given. (The *first*
    crew member is created by POST /agent/connect, not here.)"""
    updated = await auth_service.add_crew_member(
        user, data.name, data.port, data.api_key, data.color
    )
    return crew_out(updated)


@router.put("/{name}", response_model=CrewOut)
async def update_crew_member(
    name: str,
    data: CrewMemberUpdate,
    user: UserDoc = Depends(get_current_user),
):
    """Replace an existing crew member's port and key (and color, if given)."""
    updated = await auth_service.update_crew_member(
        user, name, data.port, data.api_key, data.color
    )
    return crew_out(updated)


@router.delete("/{name}", response_model=CrewOut)
async def remove_crew_member(name: str, user: UserDoc = Depends(get_current_user)):
    """Delete a named crew member. Rejected if it's the default and others
    remain — set a different default first."""
    updated = await auth_service.remove_crew_member(user, name)
    return crew_out(updated)


@router.post("/{name}/default", response_model=CrewOut)
async def set_default_crew_member(name: str, user: UserDoc = Depends(get_current_user)):
    """Make this the crew member used when a delegate/session doesn't name
    one and the alert's own `crew` doesn't match a configured name."""
    updated = await auth_service.set_default_crew_member(user, name)
    return crew_out(updated)


@router.get("/{name}/skills", response_model=list[SkillInfo])
async def list_crew_member_skills(name: str, user: UserDoc = Depends(get_current_user)):
    """This crew member's skills, read live from its own Hermes gateway.

    Never cached by gaca-api — a skill Hermes no longer has installed can
    never show up here.
    """
    return await auth_service.list_skills(user, name)
