"""HTTP layer for the GACA Cloud link. Thin: authenticate, call the service, return.

Apps and Hermes never talk to the cloud; they talk to these routes and to /api/artifacts, and this
server calls the cloud with its own stored token (which no response ever contains).

  * Managing the link (start, poll, unlink) needs a paired device: an API token can never do it,
    the same rule as managing API tokens (`get_session_user`).
  * Joining a gacabit with an invite and installing a template also need a paired device: the person
    tapping "Trust and add" IS the consent, so no API token (Hermes) can do either, whatever its scopes.
  * Reading the link status and browsing the template gallery are open to an API token that was
    explicitly given the opt-in `cloud` scope (see utils/tokens.py).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Request

from ..models.user import UserDoc
from ..schemas import artifact as artifact_schemas
from ..schemas import cloud as s
from ..services import cloud_forward, cloud_link, cloud_proxy
from ..utils.auth import get_current_user, get_session_user

router = APIRouter(prefix="/cloud", tags=["cloud"])


def _rid(value: str | None) -> str:
    return cloud_forward.clean_request_id(value)


@router.post("/link/start")
async def start_link(
    body: s.LinkStart | None = None, user: UserDoc = Depends(get_session_user)
) -> s.LinkStarted:
    """Begin linking this server to a GACA Cloud account. Open `verification_uri` in a browser
    where the person is signed in, approve the code, then call POST /cloud/link/poll every
    `interval` seconds."""
    return s.LinkStarted(**await cloud_link.start(user.id, (body or s.LinkStart()).scope))


@router.post("/link/poll")
async def poll_link(user: UserDoc = Depends(get_session_user)) -> s.LinkPoll:
    """One step of the link: `pending` (keep polling), `linked`, `expired` or `denied`."""
    return s.LinkPoll(**await cloud_link.poll(user.id))


@router.get("/link")
async def link_status(user: UserDoc = Depends(get_current_user)) -> s.LinkStatus:
    """Whether this server is linked, to which account, and with what scope. Never the token."""
    return s.LinkStatus(**await cloud_link.status(user.id))


@router.delete("/link")
async def unlink(user: UserDoc = Depends(get_session_user)) -> s.Unlinked:
    """Revoke this server's token on the cloud, then forget the link here (cloud-hosted gacabits
    disappear from this server's list; they are not deleted in the cloud). If the cloud cannot be
    reached the local link is removed anyway and `cloud_revoked` is false."""
    return s.Unlinked(**await cloud_link.unlink(user.id))


@router.post("/invites/preview")
async def preview_invite(
    body: s.InviteToken, user: UserDoc = Depends(get_session_user), x_request_id: str | None = Header(default=None)
) -> None:
    """What an invite link would add (title, role, who invited you), before joining."""
    await cloud_proxy.invite_preview(user.id, body.token, _rid(x_request_id))


@router.post("/invites/redeem")
async def redeem_invite(
    body: s.InviteToken, user: UserDoc = Depends(get_session_user), x_request_id: str | None = Header(default=None)
) -> None:
    """Join a gacabit with an invite link's token. It then shows in GET /artifacts (`home: cloud`).
    Paired device only: the person's tap is the confirmation."""
    await cloud_proxy.invite_redeem(user.id, body.token, request_id=_rid(x_request_id))


@router.get("/templates")
async def list_templates(
    request: Request, user: UserDoc = Depends(get_current_user), x_request_id: str | None = Header(default=None)
) -> None:
    """The cloud's template gallery (`q`, `category`, `tag`, `sort`, `mine`, `limit`, `cursor`)."""
    await cloud_proxy.templates(user.id, dict(request.query_params), _rid(x_request_id))


@router.get("/templates/{template_id}")
async def get_template(
    template_id: str, user: UserDoc = Depends(get_current_user), x_request_id: str | None = Header(default=None)
) -> None:
    await cloud_proxy.template(user.id, template_id, _rid(x_request_id))


@router.post("/templates/{template_id}/get")
async def get_template_bundle(
    template_id: str, body: s.TemplateGet | None = None, user: UserDoc = Depends(get_session_user),
    x_request_id: str | None = Header(default=None),
) -> None:
    """The template's bundle (html + data shapes, never data), as the cloud answers it. Only
    `target: server_bundle` exists here."""
    await cloud_proxy.template_get(user.id, template_id, _rid(x_request_id))


@router.post("/templates/{template_id}/install", status_code=201)
async def install_template(
    template_id: str, user: UserDoc = Depends(get_session_user), x_request_id: str | None = Header(default=None)
) -> artifact_schemas.ArtifactOut:
    """Get a template and create the artifact from it on this server."""
    return await cloud_proxy.template_install(user.id, template_id, _rid(x_request_id))
