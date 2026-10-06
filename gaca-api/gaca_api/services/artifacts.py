"""Artifact business logic. No FastAPI, no HTTP.

An artifact is a stateless HTML view plus the data it keeps, all scoped to the owner.
Because the data lives here and not in the page, the agent can rewrite the page without
losing anything and can read or write the same data while the page is closed.

Concurrency: the app and an agent may write at once. Keyed documents mean two adds never
touch the same row; `state` and each document carry a version, and a writer that sends
`expected_version` gets a 409 instead of silently overwriting. Read-modify-write
operations (merge patches, batches) run under one lock so they can't interleave.
"""
from __future__ import annotations

import asyncio
import copy
import json
import uuid
from typing import Any

from ..db import alerts as alerts_db
from ..db import artifacts as db
from ..models.artifact import ArtifactDoc
from ..models.base import utcnow
from ..schemas import artifact as s
from . import sessions as session_service
from .exceptions import Conflict, NotFound, PayloadTooLarge

_write_lock = asyncio.Lock()


def _json_size(value: Any) -> int:
    return len(json.dumps(value).encode())


def merge_patch(target: Any, patch: Any) -> Any:
    """RFC 7386 JSON merge patch. Returns a new value; inputs are not modified."""
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    result = dict(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = merge_patch(result.get(key), value)
    return result


def _out(row: db.ArtifactRow) -> s.ArtifactOut:
    d = row.doc
    return s.ArtifactOut(
        id=d.id, title=d.title, description=d.description, data_notes=d.data_notes, crew=d.crew,
        session_id=d.session_id, alert_id=d.alert_id, archived=row.archived, version=row.version,
        rev=row.rev, state_version=row.state_version, created_at=d.created_at,
        updated_at=row.updated_at, last_opened_at=d.last_opened_at,
    )


def _doc_out(r: db.DocRow) -> s.DocOut:
    return s.DocOut(id=r.id, version=r.version, created_at=r.created_at, updated_at=r.updated_at, data=r.data)


async def _owned(user_id: str, artifact_id: str, with_html: bool = False) -> db.ArtifactRow:
    row = await db.find(user_id, artifact_id, with_html)
    if row is None:
        raise NotFound("artifact", artifact_id)
    return row


async def _check_links(user_id: str, session_id: str | None, alert_id: str | None) -> None:
    if session_id:
        await session_service.ensure_owned(user_id, session_id)
    if alert_id:
        alert = await alerts_db.find(alert_id)
        if alert is None or alert.user_id != user_id:
            raise NotFound("alert", alert_id)


# ── artifacts ────────────────────────────────────────────────────────────────
async def create(user_id: str, body: s.ArtifactCreate) -> s.ArtifactOut:
    await _check_links(user_id, body.session_id, body.alert_id)
    if _json_size(body.state) > s.MAX_STATE_BYTES:
        raise PayloadTooLarge("state is too large")
    for name, docs in body.collections.items():
        if len(docs) > s.MAX_DOCS_PER_COLLECTION:
            raise PayloadTooLarge(f"collection {name!r} has too many documents")
        for doc_id, data in docs.items():
            if _json_size(data) > s.MAX_DOC_BYTES:
                raise PayloadTooLarge(f"document {name}/{doc_id} is too large")
    now = utcnow()
    doc = ArtifactDoc(
        user_id=user_id, title=body.title, description=body.description, data_notes=body.data_notes,
        crew=body.crew, session_id=body.session_id, alert_id=body.alert_id, created_at=now,
    )
    async with _write_lock:
        await db.insert(doc, body.html, body.state, now)
        for name, docs in body.collections.items():
            for doc_id, data in docs.items():
                await db.put_doc(doc.id, db.DocRow(name, doc_id, 1, now, now, data), True, now, commit=False)
        await db.commit()
    return _out(await _owned(user_id, doc.id))


async def list_for_user(
    user_id: str, include_archived: bool = False, session_id: str | None = None,
    alert_id: str | None = None, q: str | None = None, limit: int = 100,
) -> s.ArtifactList:
    rows = await db.find_many(user_id, include_archived, session_id, alert_id, 1000)
    if q:
        needle = q.lower()
        rows = [r for r in rows if needle in r.doc.title.lower() or needle in (r.doc.description or "").lower()]
    return s.ArtifactList(artifacts=[_out(r) for r in rows[:limit]])


async def get(user_id: str, artifact_id: str) -> s.ArtifactOut:
    return _out(await _owned(user_id, artifact_id))


async def get_html(user_id: str, artifact_id: str, version: int | None = None) -> str:
    row = await _owned(user_id, artifact_id, with_html=True)
    if version is None or version == row.version:
        return row.html or ""
    html = await db.find_version_html(artifact_id, version)
    if html is None:
        raise NotFound("artifact version", f"{artifact_id}@{version}")
    return html


async def patch(user_id: str, artifact_id: str, body: s.ArtifactPatch) -> s.ArtifactOut:
    async with _write_lock:
        row = await _owned(user_id, artifact_id)
        changes = body.model_dump(exclude_unset=True)
        update: dict[str, Any] = {}
        for key in ("title", "description", "data_notes"):
            if key in changes:
                value = changes[key]
                if key == "title":
                    if value is None:
                        continue
                    value = " ".join(value.split())
                update[key] = value
        doc = row.doc.model_copy(update=update)
        archived = row.archived if body.archived is None else body.archived
        now = utcnow()
        if body.html is not None:
            version = row.version + 1
            await db.add_version(artifact_id, version, body.note, body.html, now, s.MAX_VERSIONS_KEPT)
            await db.update_meta(doc, archived, now, html=body.html, version=version)
        else:
            await db.update_meta(doc, archived, now)
    return _out(await _owned(user_id, artifact_id))


async def restore(user_id: str, artifact_id: str, version: int) -> s.ArtifactOut:
    """Bring an old page back — as a NEW version, so history only ever grows."""
    html = await get_html(user_id, artifact_id, version)
    return await patch(user_id, artifact_id, s.ArtifactPatch(html=html, note=f"restored v{version}"))


async def versions(user_id: str, artifact_id: str) -> s.VersionList:
    row = await _owned(user_id, artifact_id)
    return s.VersionList(
        current=row.version,
        versions=[s.VersionOut(version=v["version"], created_at=v["created_at"], note=v["note"], size=v["size"])
                  for v in await db.list_versions(artifact_id)],
    )


async def mark_opened(user_id: str, artifact_id: str) -> None:
    row = await _owned(user_id, artifact_id)
    await db.touch_opened(row.doc.model_copy(update={"last_opened_at": utcnow()}))


async def delete(user_id: str, artifact_id: str) -> None:
    if not await db.delete(user_id, artifact_id):
        raise NotFound("artifact", artifact_id)


# ── state ────────────────────────────────────────────────────────────────────
async def get_state(user_id: str, artifact_id: str) -> s.StateOut:
    row = await _owned(user_id, artifact_id)
    return s.StateOut(state=row.state, version=row.state_version, rev=row.rev)


async def _write_state(row: db.ArtifactRow, new_state: dict, expected: int | None) -> s.StateOut:
    if expected is not None and expected != row.state_version:
        raise Conflict(
            f"state is at version {row.state_version}, not {expected} — reread it and apply your change again"
        )
    if _json_size(new_state) > s.MAX_STATE_BYTES:
        raise PayloadTooLarge("state is too large")
    await db.set_state(row.doc.id, new_state, row.state_version + 1, utcnow())
    return s.StateOut(state=new_state, version=row.state_version + 1, rev=row.rev + 1)


async def put_state(user_id: str, artifact_id: str, body: s.StatePut) -> s.StateOut:
    async with _write_lock:
        return await _write_state(await _owned(user_id, artifact_id), body.state, body.expected_version)


async def patch_state(user_id: str, artifact_id: str, body: s.StatePatch) -> s.StateOut:
    async with _write_lock:
        row = await _owned(user_id, artifact_id)
        return await _write_state(row, merge_patch(row.state, body.patch), body.expected_version)


# ── documents ────────────────────────────────────────────────────────────────
async def snapshot(user_id: str, artifact_id: str, since_rev: int | None) -> s.DataSnapshot:
    row = await _owned(user_id, artifact_id)
    if since_rev is not None and since_rev == row.rev:
        return s.DataSnapshot(rev=row.rev, changed=False)
    collections: dict[str, list[s.DocOut]] = {}
    for d in await db.list_docs(artifact_id):
        collections.setdefault(d.collection, []).append(_doc_out(d))
    return s.DataSnapshot(
        rev=row.rev, state=row.state, state_version=row.state_version, collections=collections
    )


async def list_collection(user_id: str, artifact_id: str, collection: str) -> s.CollectionOut:
    row = await _owned(user_id, artifact_id)
    return s.CollectionOut(
        collection=collection, rev=row.rev,
        docs=[_doc_out(d) for d in await db.list_docs(artifact_id, collection)],
    )


async def get_doc(user_id: str, artifact_id: str, collection: str, doc_id: str) -> s.DocOut:
    await _owned(user_id, artifact_id)
    d = await db.get_doc(artifact_id, collection, doc_id)
    if d is None:
        raise NotFound("document", f"{collection}/{doc_id}")
    return _doc_out(d)


def _check_doc_size(collection: str, doc_id: str, data: dict) -> None:
    if _json_size(data) > s.MAX_DOC_BYTES:
        raise PayloadTooLarge(f"document {collection}/{doc_id} is too large")


async def _set_doc(
    artifact_id: str, collection: str, doc_id: str, data: dict, expected: int | None,
    *, create_only: bool = False, commit: bool = True,
) -> db.DocRow:
    existing = await db.get_doc(artifact_id, collection, doc_id)
    if create_only and existing is not None:
        raise Conflict(f"document {collection}/{doc_id} already exists")
    if expected is not None and (existing is None or existing.version != expected):
        have = "missing" if existing is None else f"version {existing.version}"
        raise Conflict(f"{collection}/{doc_id} is {have}, not {expected} — reread it and apply your change again")
    _check_doc_size(collection, doc_id, data)
    now = utcnow()
    if existing is None:
        if await db.count_docs(artifact_id, collection) >= s.MAX_DOCS_PER_COLLECTION:
            raise PayloadTooLarge(f"collection {collection!r} is full")
        row = db.DocRow(collection, doc_id, 1, now, now, data)
    else:
        row = db.DocRow(collection, doc_id, existing.version + 1, existing.created_at, now, data)
    await db.put_doc(artifact_id, row, existing is None, now, commit=commit)
    return row


async def put_doc(user_id: str, artifact_id: str, collection: str, doc_id: str, body: s.DocPut) -> s.DocOut:
    s.check_name(collection)
    async with _write_lock:
        await _owned(user_id, artifact_id)
        return _doc_out(await _set_doc(artifact_id, collection, doc_id, body.data, body.expected_version))


async def add_doc(user_id: str, artifact_id: str, collection: str, body: s.DocCreate) -> s.DocOut:
    s.check_name(collection)
    async with _write_lock:
        await _owned(user_id, artifact_id)
        doc_id = body.id or uuid.uuid4().hex[:12]
        return _doc_out(await _set_doc(artifact_id, collection, doc_id, body.data, None, create_only=True))


async def patch_doc(user_id: str, artifact_id: str, collection: str, doc_id: str, body: s.DocPatch) -> s.DocOut:
    s.check_name(collection)
    async with _write_lock:
        await _owned(user_id, artifact_id)
        existing = await db.get_doc(artifact_id, collection, doc_id)
        if existing is None:
            raise NotFound("document", f"{collection}/{doc_id}")
        merged = merge_patch(existing.data, body.patch)
        return _doc_out(await _set_doc(artifact_id, collection, doc_id, merged, body.expected_version))


async def delete_doc(user_id: str, artifact_id: str, collection: str, doc_id: str) -> None:
    async with _write_lock:
        await _owned(user_id, artifact_id)
        if not await db.delete_docs(artifact_id, collection, doc_id, utcnow()):
            raise NotFound("document", f"{collection}/{doc_id}")


async def clear_collection(user_id: str, artifact_id: str, collection: str) -> int:
    async with _write_lock:
        await _owned(user_id, artifact_id)
        return await db.delete_docs(artifact_id, collection, None, utcnow())


async def batch(user_id: str, artifact_id: str, body: s.Batch) -> s.BatchResult:
    """All-or-nothing: if any op fails, none of them are applied."""
    async with _write_lock:
        await _owned(user_id, artifact_id)
        try:
            for op in body.ops:
                if op.op == "set":
                    await _set_doc(artifact_id, op.collection, op.id, op.data, None, commit=False)
                elif op.op == "patch":
                    existing = await db.get_doc(artifact_id, op.collection, op.id)
                    if existing is None:
                        raise NotFound("document", f"{op.collection}/{op.id}")
                    await _set_doc(artifact_id, op.collection, op.id, merge_patch(existing.data, op.data), None, commit=False)
                elif op.op == "delete":
                    if not await db.delete_docs(artifact_id, op.collection, op.id, utcnow(), commit=False):
                        raise NotFound("document", f"{op.collection}/{op.id}")
                else:
                    await db.delete_docs(artifact_id, op.collection, None, utcnow(), commit=False)
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        row = await _owned(user_id, artifact_id)
    return s.BatchResult(applied=len(body.ops), rev=row.rev)


# ── events ───────────────────────────────────────────────────────────────────
async def add_event(user_id: str, artifact_id: str, body: s.EventIn) -> s.EventOut:
    await _owned(user_id, artifact_id)
    if _json_size(body.payload) > s.MAX_DOC_BYTES:
        raise PayloadTooLarge("event payload is too large")
    now = utcnow()
    seq = await db.add_event(artifact_id, user_id, body.name, body.payload, now)
    return s.EventOut(seq=seq, name=body.name, created_at=now, handled=False, payload=body.payload)


async def list_events(
    user_id: str, artifact_id: str, after: int, unhandled_only: bool, limit: int
) -> s.EventList:
    await _owned(user_id, artifact_id)
    return s.EventList(events=[s.EventOut(**e) for e in await db.list_events(artifact_id, after, unhandled_only, limit)])


async def mark_events_handled(user_id: str, artifact_id: str, upto: int) -> s.HandledResult:
    await _owned(user_id, artifact_id)
    return s.HandledResult(handled=await db.mark_events_handled(artifact_id, upto))


