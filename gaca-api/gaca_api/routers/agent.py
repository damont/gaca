"""HTTP layer for the user's one connected Hermes agent.

An account connects to exactly one agent — one box, one host. What that
account then does with it (add more crew members, i.e. more Hermes profiles
on the same box) is routers/crew.py's job; this router only covers "is there
an agent, connect one, move it to a new host." Deliberately doesn't say
"port" or "process" anywhere in a docstring or response a client would read
— that's Hermes plumbing (see gaca-planning/hermes/0002-storage-boundary.md),
not something this API's shape should make the caller think about up front.

Same rule applies to /provisioning below: the contract is "can gaca-api set
up crew members on this box for you," never "SSH." Whatever gaca-api
actually uses to reach the box (SSH today; see
gaca-planning/hermes/0003-ssh-provisioning.md) is free to change without
this router's paths, schema names, or a caller's mental model changing —
the same reason AgentBackend hides which engine is Hermes.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, status

from ..models.user import UserDoc
from ..provisioning.ssh import SSHError
from ..schemas.auth import (
    AgentConnect,
    AgentHostUpdate,
    AgentStatus,
    CrewOut,
    ProvisioningAccess,
    ProvisioningStatus,
    ProvisioningTestResult,
)
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


@router.get("/provisioning", response_model=ProvisioningStatus)
async def provisioning_status(user: UserDoc = Depends(get_current_user)):
    """Can gaca-api set up new crew members on this box directly, rather
    than requiring one to be created and entered by hand. Never returns the
    access credential itself — same rule as a password."""
    ssh = user.ssh
    return ProvisioningStatus(
        enabled=ssh is not None,
        host=ssh.host if ssh else None,
        username=ssh.username if ssh else None,
    )


@router.put("/provisioning", response_model=ProvisioningStatus)
async def set_provisioning_access(
    data: ProvisioningAccess, user: UserDoc = Depends(get_current_user)
):
    """Grant gaca-api access to set up new crew members on this box, so
    POST /crew can create one directly instead of requiring a
    manually-configured one. Not verified here — see POST /provisioning/test."""
    updated = await auth_service.set_ssh_connection(
        user, data.host, data.port, data.username, data.private_key
    )
    ssh = updated.ssh
    return ProvisioningStatus(enabled=True, host=ssh.host, username=ssh.username)


@router.post("/provisioning/test", response_model=ProvisioningTestResult)
async def test_provisioning_access(user: UserDoc = Depends(get_current_user)):
    """Actually reach the box with the stored access and confirm it works,
    so a typo'd credential or unreachable box surfaces immediately rather
    than the first time it matters (creating a crew member)."""
    try:
        await auth_service.test_ssh_connection(user)
        return ProvisioningTestResult(ok=True, message="Connected.")
    except SSHError as exc:
        return ProvisioningTestResult(ok=False, message=str(exc))


@router.post("/discover", response_model=CrewOut)
async def discover_crew(user: UserDoc = Depends(get_current_user)):
    """Find crew members that already exist on the box — set up by hand, or
    by anything other than this account's own POST /crew — and add them, so
    pre-existing infrastructure doesn't have to be re-entered one field at a
    time. Read-only against the box; never overwrites a crew member already
    known by that name. Requires provisioning access (`PUT /provisioning`)
    first — `400` with CrewNotConfigured if none is stored yet.
    """
    updated = await auth_service.discover_crew(user)
    return crew_out(updated)
