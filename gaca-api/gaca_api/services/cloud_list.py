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

import asyncio
import logging
import re
import time
from typing import Any

import httpx

from ..config import settings
from ..db import artifacts as artifacts_db
from ..db import cloud as db
from ..db import cloud_move as move_db
from ..models.base import utcnow
from ..models.cloud import CloudPointer
from ..schemas import artifact as s
from ..schemas import cloud as cs
from . import artifacts as svc
from . import artifact_locks, cloud_link
from .exceptions import CloudError, CloudReply, NotFound

logger = logging.getLogger(__name__)

_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_PAGE_LIMIT = 500
_MAX_PAGES = 10
_synced: dict[str, float] = {}  # user_id -> time.monotonic() of the last good sync
_failed: dict[str, float] = {}  # user_id -> time.monotonic() of the last failed sync
_locks: dict[str, asyncio.Lock] = {}  # one sync at a time per user


def forget(user_id: str) -> None:
    """Drop the 'recently synced / failed' memory so the next list asks the cloud again."""
    _synced.pop(user_id, None)
    _failed.pop(user_id, None)


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
    out["moving"] = False
    out["cloud_status"] = g.get("status") if isinstance(g.get("status"), str) else None
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
async def _fetch_all(user_id: str) -> tuple[list[dict], bool]:
    """(gacabits, complete). `complete` is False when the cloud had more pages than we are willing to read."""
    items: list[dict] = []
    cursor: str | None = None
    timeout = httpx.Timeout(settings.cloud_timeout_seconds, connect=settings.cloud_list_connect_timeout_seconds)
    for _ in range(_MAX_PAGES):
        params: dict = {"scope": "all", "include_archived": "true", "limit": _PAGE_LIMIT}
        if cursor:
            params["cursor"] = cursor
        resp = await cloud_link.call(user_id, "GET", "/gacabits", params=params, timeout=timeout)
        data = cloud_link._json(resp)
        if not isinstance(data, dict) or not isinstance(data.get("gacabits"), list):
            raise CloudError("The cloud answered in a way this server did not understand.", code="cloud_unavailable")
        items += [g for g in data["gacabits"] if isinstance(g, dict)]
        cursor = data.get("next_cursor")
        if not cursor:
            return items, True
    return items, False


def _fresh(user_id: str) -> bool:
    return time.monotonic() - _synced.get(user_id, -1e9) < settings.cloud_list_ttl_seconds


def _backing_off(user_id: str) -> bool:
    return time.monotonic() - _failed.get(user_id, -1e9) < settings.cloud_list_backoff_seconds


async def pointers_for_list(user_id: str) -> tuple[list[CloudPointer], bool]:
    """(pointers, stale). Asks the cloud at most every `cloud_list_ttl_seconds`, one request at a
    time however many lists are asked for at once; if it cannot be asked, answers from what this
    server last saw, marked stale, and does not try again for `cloud_list_backoff_seconds` (so a
    cloud outage does not make every list wait out a timeout)."""
    link = await db.get_link(user_id)
    if link is None or link.status == "pending":
        return [], False
    if link.status == "invalid":
        return await db.list_pointers(user_id), True
    if _fresh(user_id):
        return await db.list_pointers(user_id), False
    if _backing_off(user_id):
        return await db.list_pointers(user_id), True
    async with _locks.setdefault(user_id, asyncio.Lock()):
        # Whoever held the lock before us may have done the work (or learnt the cloud is down).
        if _fresh(user_id):
            return await db.list_pointers(user_id), False
        if _backing_off(user_id):
            return await db.list_pointers(user_id), True
        try:
            items, complete = await _fetch_all(user_id)
        except (CloudError, CloudReply) as e:
            _failed[user_id] = time.monotonic()
            logger.info("cloud list unavailable, serving the last known list (%s)", getattr(e, "code", "reply"))
            return await db.list_pointers(user_id), True
        started = time.monotonic()
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
        if complete:  # never prune on a partial list: a gacabit on a page we did not read is not gone
            for gone in set(existing) - seen:
                await db.delete_pointer(user_id, gone, commit=False)
        await db.commit()
        _synced[user_id] = started
        _failed.pop(user_id, None)
        return await db.list_pointers(user_id), False


# ── local rows, with where they are in a cloud transfer ──────────────────────
async def _transfer_states(user_id: str) -> tuple[dict[str, dict], set[str]]:
    """(failed move attempts by artifact id, ids that came home and are waiting for the cloud to be told)."""
    failed = await move_db.failures(user_id)
    waiting = {r["artifact_id"] for r in await move_db.list_bring_homes(user_id) if not r["confirmed"]}
    return failed, waiting


def _mark(item: cs.ArtifactListItem, failed: dict[str, dict], waiting: set[str]) -> cs.ArtifactListItem:
    if item.id in artifact_locks.MOVING:
        item.moving, item.cloud_status = True, "moving"
    elif item.id in failed:
        item.cloud_status = "move_failed"
    elif item.id in waiting:
        item.cloud_status = "bring_home_unconfirmed"
    return item


async def local_item(user_id: str, out: s.ArtifactOut) -> cs.ArtifactListItem:
    """One local artifact as a list item, with its transfer state (used by GET /artifacts/{id})."""
    failed, waiting = await _transfer_states(user_id)
    return _mark(cs.ArtifactListItem.model_validate(out.model_dump()), failed, waiting)


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
    q: str | None = None, limit: int = 100, include_cloud: bool = True,
) -> cs.ArtifactListMerged:
    """`include_cloud` False (an API token without the `cloud` scope): local artifacts only, exactly
    as before the cloud existed."""
    pointers, stale = ([], False)
    if include_cloud and not (session_id or alert_id):
        pointers, stale = await pointers_for_list(user_id)
    # With nothing to merge the local query can stop at `limit`; with pointers it must see them all.
    local = await svc.list_for_user(user_id, include_archived, session_id, alert_id, q, 1000 if pointers else limit)
    failed, waiting = await _transfer_states(user_id)
    items = [_mark(cs.ArtifactListItem.model_validate(a.model_dump()), failed, waiting) for a in local.artifacts]
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


async def reorder(user_id: str, ids: list[str], include_cloud: bool = True) -> cs.ArtifactListMerged:
    """PUT /order over both kinds. Without any pointer this is exactly the local reorder."""
    pointers = await db.list_pointers(user_id) if include_cloud else []
    if not pointers:
        await svc.reorder(user_id, ids)
        return await merged(user_id, include_cloud=include_cloud)
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
    return await merged(user_id, include_cloud=include_cloud)
