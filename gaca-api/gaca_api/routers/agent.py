"""HTTP layer for the user's one connected Hermes agent.

An account connects to exactly one agent — one box, one host. What that
account then does with it (add more crew members, i.e. more Hermes profiles
on the same box, or have gaca-api create one directly) is routers/crew.py's
job; this router only covers "is there an agent, connect one, move it to a
new host." Deliberately doesn't say "port" or "process" anywhere in a
docstring or response a client would read — that's Hermes plumbing (see
gaca-planning/hermes/0002-storage-boundary.md), not something this API's
shape should make the caller think about up front.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, status

from ..models.user import UserDoc
from ..schemas.auth import AgentConnect, AgentHostUpdate, AgentStatus, CrewOut
from ..services import auth as auth_service
from ..utils.auth import get_current_user
from .crew import crew_out

router = APIRouter(prefix="/agent", tags=["agent"])


@router.get("", response_model=AgentStatus)
async def agent_status(user: UserDoc = Depends(get_current_user)):
    """Is this account connected to an agent yet, and where."""
    return AgentStatus(connected=user.agent_host is not None, host=user.agent_host)


@router.post("/connect", status_code=status.HTTP_201_CREATED, response_model=CrewOut)
async def connect_agent(
    data: AgentConnect,
    user: UserDoc = Depends(get_current_user),
):
    """First-time setup: connect to your Hermes agent.

    Creates `name` (default: "default") as the first crew member from the
    given port/key, so there's already someone to chat with. Returns the
    full crew picture, same shape `GET /crew` does. `422` if this account is
    already connected — see `PUT /agent` to move to a new host instead.
    """
    updated = await auth_service.connect_agent(
        user, data.host, data.port, data.api_key, data.name
    )
    return crew_out(updated)


@router.put("", response_model=CrewOut)
async def update_agent_host(
    data: AgentHostUpdate,
    user: UserDoc = Depends(get_current_user),
):
    """Move the connected agent (and every crew member under it) to a new
    host — the box's IP changed, a new Cloudflare tunnel, etc. Leaves the
    crew list itself untouched."""
    updated = await auth_service.set_agent_host(user, data.host)
    return crew_out(updated)
