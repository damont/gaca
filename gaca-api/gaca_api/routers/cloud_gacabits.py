"""Sharing a cloud page, publishing it, and managing the account's templates: an ALLOWLIST of
pass-throughs to the GACA Cloud (Part 8b). Every route here needs a PAIRED DEVICE: an API token
(Hermes) is refused whatever its scopes, even `cloud`, because the person's tap in the app is the
confirmation the cloud wants from a server (`user_confirmed`, added by services/cloud_share.py).

Nothing here is a general proxy. Each route is declared one by one, its path ids are validated before
anything is sent (a gacabit/template id is 32 lowercase hex; account/invite ids are plain tokens), and
the cloud path is built from those validated values only. The cloud decides who may do what and answers
with its own status and body.

An invite link's `url` is returned once, exactly as the cloud sends it (`Cache-Control: no-store`), and
this server never keeps or logs it.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Path, Request

from ..models.user import UserDoc
from ..services import cloud_client, cloud_forward, cloud_share
from ..utils.auth import get_session_user

router = APIRouter(prefix="/cloud", tags=["cloud-sharing"])

_HEX32 = r"^[0-9a-f]{32}$"
_TOKEN = r"^[A-Za-z0-9_-]{1,64}$"
GID = Path(pattern=_HEX32, description="The gacabit id (32 lowercase hex)")
TID = Path(pattern=_HEX32, description="The template id (32 lowercase hex)")
ACCOUNT = Path(pattern=_TOKEN)
INVITE = Path(pattern=_TOKEN)


def _g(gid: str, tail: str = "") -> str:
    return f"/gacabits/{cloud_client.quote_id(gid)}{tail}"


def _t(tid: str, tail: str = "") -> str:
    return f"/templates/{cloud_client.quote_id(tid)}{tail}"


def _rid(value: str | None) -> str:
    return cloud_forward.clean_request_id(value)


# ── members ──────────────────────────────────────────────────────────────────
@router.get("/gacabits/{gid}/members")
async def list_members(
    gid: str = GID, user: UserDoc = Depends(get_session_user), x_request_id: str | None = Header(default=None)
) -> None:
    """Members, and (owner only) the active invite links (never their secret)."""
    await cloud_share.forward(user.id, "GET", _g(gid, "/members"), request_id=_rid(x_request_id))


@router.patch("/gacabits/{gid}/members/{account_id}")
async def change_member(
    request: Request, gid: str = GID, account_id: str = ACCOUNT, user: UserDoc = Depends(get_session_user),
    x_request_id: str | None = Header(default=None),
) -> None:
    """Change a member's role (owner). Body: `{role: viewer|editor|maintainer}`."""
    await cloud_share.forward(
        user.id, "PATCH", _g(gid, f"/members/{cloud_client.quote_id(account_id)}"), raw_body=await request.body(),
        request_id=_rid(x_request_id))


@router.delete("/gacabits/{gid}/members/{account_id}")
async def remove_member(
    gid: str = GID, account_id: str = ACCOUNT, user: UserDoc = Depends(get_session_user),
    x_request_id: str | None = Header(default=None),
) -> None:
    await cloud_share.forward(
        user.id, "DELETE", _g(gid, f"/members/{cloud_client.quote_id(account_id)}"), request_id=_rid(x_request_id))


@router.post("/gacabits/{gid}/leave")
async def leave(
    gid: str = GID, user: UserDoc = Depends(get_session_user), x_request_id: str | None = Header(default=None)
) -> None:
    """A member leaves (the owner gets the cloud's `409 owner_cannot_leave`). The page disappears from the list."""
    await cloud_share.leave(user.id, gid, _rid(x_request_id))


# ── invite links ─────────────────────────────────────────────────────────────
@router.get("/gacabits/{gid}/invites")
async def list_invites(
    gid: str = GID, user: UserDoc = Depends(get_session_user), x_request_id: str | None = Header(default=None)
) -> None:
    await cloud_share.forward(user.id, "GET", _g(gid, "/invites"), request_id=_rid(x_request_id))


@router.post("/gacabits/{gid}/invites")
async def create_invite(
    request: Request, gid: str = GID, user: UserDoc = Depends(get_session_user),
    x_request_id: str | None = Header(default=None),
) -> None:
    """Create an invite link (`{role, expires_in_days?, max_uses?}`). The answer holds the link's `url` ONCE;
    it is not kept here, so show or copy it now."""
    await cloud_share.forward(
        user.id, "POST", _g(gid, "/invites"), raw_body=await request.body(), confirm_body=True, no_store=True,
        request_id=_rid(x_request_id))


@router.delete("/gacabits/{gid}/invites/{invite_id}")
async def revoke_invite(
    gid: str = GID, invite_id: str = INVITE, user: UserDoc = Depends(get_session_user),
    x_request_id: str | None = Header(default=None),
) -> None:
    await cloud_share.forward(
        user.id, "DELETE", _g(gid, f"/invites/{cloud_client.quote_id(invite_id)}"), request_id=_rid(x_request_id))


# ── share / publish previews, publish ────────────────────────────────────────
@router.get("/gacabits/{gid}/validations")
async def list_validations(
    gid: str = GID, user: UserDoc = Depends(get_session_user), x_request_id: str | None = Header(default=None)
) -> None:
    await cloud_share.forward(user.id, "GET", _g(gid, "/validations"), request_id=_rid(x_request_id))


@router.post("/gacabits/{gid}/validations")
async def run_validation(
    request: Request, gid: str = GID, user: UserDoc = Depends(get_session_user),
    x_request_id: str | None = Header(default=None),
) -> None:
    """Run the `share` or `publish` checks now (`{subject: share|publish, ...listing fields}`). The report is
    final in the answer; for `publish` it carries the preview and the hash to confirm."""
    await cloud_share.forward(
        user.id, "POST", _g(gid, "/validations"), raw_body=await request.body(), request_id=_rid(x_request_id))


@router.post("/gacabits/{gid}/publish")
async def publish(
    request: Request, gid: str = GID, user: UserDoc = Depends(get_session_user),
    x_request_id: str | None = Header(default=None),
) -> None:
    """Publish a stripped template (html + schemas, never data), after the person confirmed the preview."""
    await cloud_share.forward(
        user.id, "POST", _g(gid, "/publish"), raw_body=await request.body(), confirm_body=True,
        request_id=_rid(x_request_id))


# ── the account's own templates (the gallery itself is read in routers/cloud.py) ──
@router.patch("/templates/{tid}")
async def patch_template(
    request: Request, tid: str = TID, user: UserDoc = Depends(get_session_user),
    x_request_id: str | None = Header(default=None),
) -> None:
    """Edit the listing text or list/unlist a template you published."""
    await cloud_share.forward(
        user.id, "PATCH", _t(tid), raw_body=await request.body(), confirm_body=True, request_id=_rid(x_request_id))


@router.delete("/templates/{tid}")
async def delete_template(
    tid: str = TID, user: UserDoc = Depends(get_session_user), x_request_id: str | None = Header(default=None)
) -> None:
    """Remove a template you published (pages already made from it are unaffected)."""
    await cloud_share.forward(user.id, "DELETE", _t(tid), confirm_query=True, request_id=_rid(x_request_id))


@router.post("/templates/{tid}/recheck")
async def recheck_template(
    tid: str = TID, user: UserDoc = Depends(get_session_user), x_request_id: str | None = Header(default=None)
) -> None:
    await cloud_share.forward(user.id, "POST", _t(tid, "/recheck"), request_id=_rid(x_request_id))
