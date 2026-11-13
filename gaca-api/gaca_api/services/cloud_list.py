"""Cloud gacabits in this server's artifact list: pointers, the merged list, and ordering.

A pointer (db/cloud.py `cloud_pointers`) is all this server keeps of a cloud gacabit: the same id,
the caller's role, who shared it, and the last metadata seen. The data stays in the cloud and is
forwarded (services/cloud_forward.py). Pointers are made by listing (a sync against the cloud's
GET /gacabits) and by redeeming an invite; they are deleted when the cloud says the caller has no
access any more, when the caller leaves, and when the link is removed.

The merged list keeps every existing field and the existing ordering rules (the owner's manual
position first, never-placed pages above those, newest first). With no pointers it returns exactly
what the local list always did.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any

from ..config import settings
from ..db import artifacts as artifacts_db
from ..db import cloud as db
from ..models.base import utcnow
from ..models.cloud import CloudPointer
from ..schemas import artifact as s
from ..schemas import cloud as cs
from . import artifacts as svc
from . import cloud_link
from .exceptions import CloudError, CloudReply, NotFound

logger = logging.getLogger(__name__)

_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_PAGE_LIMIT = 500
_MAX_PAGES = 10
_synced: dict[str, float] = {}  # user_id -> time.monotonic() of the last good sync


def forget(user_id: str) -> None:
    """Drop the 'recently synced' memory so the next list asks the cloud again."""
    _synced.pop(user_id, None)


# ── gacabit JSON <-> pointer ─────────────────────────────────────────────────
def shared_by(g: dict) -> str | None:
    owner = g.get("owner")
    if g.get("role") != "owner" and isinstance(owner, dict) and isinstance(owner.get("handle"), str):
        return owner["handle"]
    return None


def adapt(g: dict, *, position: int | None = None) -> dict:
    """A cloud Gacabit as an artifact: every key the cloud sent stays as it was, the keys an app
    expects from an artifact are filled in when missing, `link` uses the in-app address apps know,
    and `home`, `shared_by` say where it lives."""
    out = dict(g)
    for key, default in (
        ("description", None), ("data_notes", None), ("crew", None), ("session_id", None), ("alert_id", None),
        ("archived", False), ("last_opened_at", None), ("schema_issues", []), ("schemas", {}),
    ):
        out.setdefault(key, default)
    out.setdefault("position", position)
    out["link"] = f"gaca://artifact/{g.get('id', '')}"
    out["home"] = "cloud"
    out["shared_by"] = shared_by(g)
    out.setdefault("member_count", 1)
    return out


def pointer_from(user_id: str, account_id: str, g: Any, position: int | None = None) -> CloudPointer | None:
    if not isinstance(g, dict):
        return None
    gid, title, role = g.get("id"), g.get("title"), g.get("role")
    if not (isinstance(gid, str) and _ID.match(gid) and isinstance(title, str) and isinstance(role, str)):
        return None
    count = g.get("member_count")
    return CloudPointer(
        id=gid, user_id=user_id, cloud_id=gid, account_id=account_id, role=role, shared_by=shared_by(g),
        title=title, member_count=count if isinstance(count, int) and count >= 1 else 1,
        position=position, last_seen=utcnow(), meta=g,
    )


async def remember(user_id: str, g: Any) -> CloudPointer | None:
    """Create or refresh the pointer for this cloud gacabit. A local artifact with the same id
    wins (it is never hidden behind a pointer), so then there is no pointer."""
    link = await cloud_link.require_link(user_id)
    p = pointer_from(user_id, link.account_id or "", g)
    if p is None:
        return None
    if await artifacts_db.find(user_id, p.id) is not None:
        return None
    await db.upsert_pointer(p)
    return await db.get_pointer(user_id, p.id)


# ── sync ─────────────────────────────────────────────────────────────────────
async def _fetch_all(user_id: str) -> list[dict]:
    items: list[dict] = []
    cursor: str | None = None
    for _ in range(_MAX_PAGES):
        params: dict = {"scope": "all", "include_archived": "true", "limit": _PAGE_LIMIT}
        if cursor:
            params["cursor"] = cursor
        resp = await cloud_link.call(user_id, "GET", "/gacabits", params=params)
        data = cloud_link._json(resp)
        if not isinstance(data, dict) or not isinstance(data.get("gacabits"), list):
            raise CloudError("The cloud answered in a way this server did not understand.", code="cloud_unavailable")
        items += [g for g in data["gacabits"] if isinstance(g, dict)]
        cursor = data.get("next_cursor")
        if not cursor:
            return items
    return items  # more than _MAX_PAGES * _PAGE_LIMIT: use what we have, never prune on a partial list


async def pointers_for_list(user_id: str) -> tuple[list[CloudPointer], bool]:
    """(pointers, stale). Asks the cloud at most every `cloud_list_ttl_seconds`; if it cannot be
    asked, answers from what this server last saw, marked stale."""
    link = await db.get_link(user_id)
    if link is None or link.status == "pending":
        return [], False
    if link.status == "invalid":
        return await db.list_pointers(user_id), True
    now = time.monotonic()
    if now - _synced.get(user_id, -1e9) < settings.cloud_list_ttl_seconds:
        return await db.list_pointers(user_id), False
    try:
        items = await _fetch_all(user_id)
    except (CloudError, CloudReply) as e:
        logger.info("cloud list unavailable, serving the last known list (%s)", getattr(e, "code", "reply"))
        return await db.list_pointers(user_id), True
    existing = {p.id: p for p in await db.list_pointers(user_id)}
    seen: set[str] = set()
    for g in items:
        p = pointer_from(user_id, link.account_id or "", g)
        if p is None or await artifacts_db.find(user_id, p.id) is not None:
            continue
        old = existing.get(p.id)
        if old:
            p.position = old.position
        await db.upsert_pointer(p, commit=False)
        seen.add(p.id)
    for gone in set(existing) - seen:
        await db.delete_pointer(user_id, gone, commit=False)
    await db.commit()
    _synced[user_id] = now
    return await db.list_pointers(user_id), False


# ── the merged list ──────────────────────────────────────────────────────────
def _item(p: CloudPointer) -> cs.ArtifactListItem:
    g = adapt(p.meta, position=p.position)
    g["position"] = p.position
    g["role"], g["member_count"] = p.role, p.member_count
    g["title"] = p.title
    try:
        return cs.ArtifactListItem.model_validate(g)
    except Exception:  # a gacabit the cloud described oddly must not break everyone's list
        return cs.ArtifactListItem(
            id=p.id, title=p.title, version=int(g.get("version") or 1), rev=int(g.get("rev") or 0),
            state_version=int(g.get("state_version") or 0), created_at=p.last_seen, updated_at=p.last_seen,
            home="cloud", role=p.role, shared_by=p.shared_by, member_count=p.member_count, position=p.position)


def _order(items: list[cs.ArtifactListItem]) -> list[cs.ArtifactListItem]:
    """The local rule, over both kinds: placed ones first by position, then never-placed ones, each newest first."""
    items = sorted(items, key=lambda i: i.id)
    items.sort(key=lambda i: i.updated_at, reverse=True)
    items.sort(key=lambda i: (i.position is not None, i.position if i.position is not None else 0))
    return items


async def merged(
    user_id: str, include_archived: bool = False, session_id: str | None = None, alert_id: str | None = None,
    q: str | None = None, limit: int = 100,
) -> cs.ArtifactListMerged:
    local = await svc.list_for_user(user_id, include_archived, session_id, alert_id, q, 1000)
    items = [cs.ArtifactListItem.model_validate(a.model_dump()) for a in local.artifacts]
    pointers, stale = ([], False) if (session_id or alert_id) else await pointers_for_list(user_id)
    if pointers:
        needle = q.lower() if q else None
        for p in pointers:
            if not include_archived and p.meta.get("archived"):
                continue
            if needle and needle not in p.title.lower() and needle not in str(p.meta.get("description") or "").lower():
                continue
            items.append(_item(p))
        items = _order(items)
    return cs.ArtifactListMerged(artifacts=items[:limit], stale=stale)


async def reorder(user_id: str, ids: list[str]) -> cs.ArtifactListMerged:
    """PUT /order over both kinds. Without any pointer this is exactly the local reorder."""
    pointers = await db.list_pointers(user_id)
    if not pointers:
        await svc.reorder(user_id, ids)
        return await merged(user_id)
    local = await svc.list_for_user(user_id, True, None, None, None, 100000)
    everything = _order(
        [cs.ArtifactListItem.model_validate(a.model_dump()) for a in local.artifacts] + [_item(p) for p in pointers])
    known = {i.id for i in everything}
    wanted = list(dict.fromkeys(ids))
    for artifact_id in wanted:
        if artifact_id not in known:
            raise NotFound("artifact", artifact_id)
    order = wanted + [i.id for i in everything if i.id not in set(wanted)]
    pointer_ids = {p.id for p in pointers}
    await db.set_positions(user_id, [(i, n) for n, i in enumerate(order) if i in pointer_ids])
    await artifacts_db.set_positions_at(user_id, [(i, n) for n, i in enumerate(order) if i not in pointer_ids])
    return await merged(user_id)
