"""Data access for the artifact tables. Plain SQL; the rules live in services/artifacts.py."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from ..models.artifact import ArtifactDoc
from .client import connection


@dataclass
class ArtifactRow:
    doc: ArtifactDoc
    archived: bool
    version: int
    state: dict
    state_version: int
    rev: int
    updated_at: datetime
    position: int | None = None  # the owner's manual order; None = never placed
    html: str | None = None  # only when asked for (it's the big column)


@dataclass
class DocRow:
    collection: str
    id: str
    version: int
    created_at: datetime
    updated_at: datetime
    data: dict


def _row(r, with_html: bool = False) -> ArtifactRow:
    return ArtifactRow(
        doc=ArtifactDoc.model_validate_json(r["doc"]),
        archived=bool(r["archived"]),
        version=r["version"],
        state=json.loads(r["state"]),
        state_version=r["state_version"],
        rev=r["rev"],
        updated_at=datetime.fromisoformat(r["updated_at"]),
        position=r["position"],
        html=r["html"] if with_html else None,
    )


def _doc_row(r) -> DocRow:
    return DocRow(
        collection=r["collection"],
        id=r["doc_id"],
        version=r["version"],
        created_at=datetime.fromisoformat(r["created_at"]),
        updated_at=datetime.fromisoformat(r["updated_at"]),
        data=json.loads(r["data"]),
    )


_META = "id, user_id, archived, version, state, state_version, rev, updated_at, position, doc"


# ── artifacts ────────────────────────────────────────────────────────────────
async def insert(doc: ArtifactDoc, html: str, state: dict, now: datetime) -> None:
    conn = connection()
    await conn.execute(
        "INSERT INTO artifacts (id, user_id, version, state, state_version, rev, updated_at, doc, html) "
        "VALUES (?, ?, 1, ?, ?, 0, ?, ?, ?)",
        (doc.id, doc.user_id, json.dumps(state), 1 if state else 0, now.isoformat(), doc.model_dump_json(), html),
    )
    await conn.execute(
        "INSERT INTO artifact_versions (artifact_id, version, created_at, note, html) VALUES (?, 1, ?, ?, ?)",
        (doc.id, now.isoformat(), "created", html),
    )
    await conn.commit()


async def find(user_id: str, artifact_id: str, with_html: bool = False) -> ArtifactRow | None:
    cols = _META + (", html" if with_html else "")
    async with connection().execute(
        f"SELECT {cols} FROM artifacts WHERE id = ? AND user_id = ?", (artifact_id, user_id)
    ) as cursor:
        row = await cursor.fetchone()
    return _row(row, with_html) if row else None


async def find_many(
    user_id: str, include_archived: bool, session_id: str | None, alert_id: str | None, limit: int
) -> list[ArtifactRow]:
    sql = f"SELECT {_META} FROM artifacts WHERE user_id = ?"
    params: list = [user_id]
    if not include_archived:
        sql += " AND archived = 0"
    # session / alert live inside `doc`; one user's artifact count is small enough that
    # filtering after the (indexed) per-user read beats another column.
    # The owner's manual order first; anything never placed (a brand-new page) sorts above it,
    # newest first, until they drag it somewhere.
    sql += " ORDER BY (position IS NOT NULL), position, updated_at DESC, id"
    async with connection().execute(sql, params) as cursor:
        rows = [_row(r) for r in await cursor.fetchall()]
    if session_id:
        rows = [r for r in rows if r.doc.session_id == session_id]
    if alert_id:
        rows = [r for r in rows if r.doc.alert_id == alert_id]
    return rows[:limit]


async def set_positions(user_id: str, ordered_ids: list[str]) -> None:
    """Number the given artifacts 0..n-1 in that order (rows not listed are left alone)."""
    conn = connection()
    await conn.executemany(
        "UPDATE artifacts SET position = ? WHERE id = ? AND user_id = ?",
        [(i, artifact_id, user_id) for i, artifact_id in enumerate(ordered_ids)],
    )
    await conn.commit()


async def update_meta(
    doc: ArtifactDoc, archived: bool, updated_at: datetime, html: str | None = None,
    version: int | None = None,
) -> None:
    conn = connection()
    if html is None:
        await conn.execute(
            "UPDATE artifacts SET doc = ?, archived = ?, updated_at = ? WHERE id = ?",
            (doc.model_dump_json(), int(archived), updated_at.isoformat(), doc.id),
        )
    else:
        await conn.execute(
            "UPDATE artifacts SET doc = ?, archived = ?, updated_at = ?, html = ?, version = ? WHERE id = ?",
            (doc.model_dump_json(), int(archived), updated_at.isoformat(), html, version, doc.id),
        )
    await conn.commit()


async def touch_opened(doc: ArtifactDoc) -> None:
    """Record an open without bumping `updated_at` (opening isn't a change)."""
    conn = connection()
    await conn.execute("UPDATE artifacts SET doc = ? WHERE id = ?", (doc.model_dump_json(), doc.id))
    await conn.commit()


async def delete(user_id: str, artifact_id: str) -> bool:
    conn = connection()
    cursor = await conn.execute("DELETE FROM artifacts WHERE id = ? AND user_id = ?", (artifact_id, user_id))
    deleted = cursor.rowcount > 0
    if deleted:
        for table in ("artifact_versions", "artifact_docs", "artifact_events"):
            await conn.execute(f"DELETE FROM {table} WHERE artifact_id = ?", (artifact_id,))
    await conn.commit()
    return deleted


# ── versions ─────────────────────────────────────────────────────────────────
async def add_version(artifact_id: str, version: int, note: str | None, html: str, now: datetime, keep: int) -> None:
    conn = connection()
    await conn.execute(
        "INSERT INTO artifact_versions (artifact_id, version, created_at, note, html) VALUES (?, ?, ?, ?, ?)",
        (artifact_id, version, now.isoformat(), note, html),
    )
    await conn.execute(
        "DELETE FROM artifact_versions WHERE artifact_id = ? AND version <= ?", (artifact_id, version - keep)
    )
    await conn.commit()


async def list_versions(artifact_id: str) -> list[dict]:
    async with connection().execute(
        "SELECT version, created_at, note, length(html) AS size FROM artifact_versions "
        "WHERE artifact_id = ? ORDER BY version DESC",
        (artifact_id,),
    ) as cursor:
        return [dict(r) for r in await cursor.fetchall()]


async def find_version_html(artifact_id: str, version: int) -> str | None:
    async with connection().execute(
        "SELECT html FROM artifact_versions WHERE artifact_id = ? AND version = ?", (artifact_id, version)
    ) as cursor:
        row = await cursor.fetchone()
    return row["html"] if row else None


# ── state ────────────────────────────────────────────────────────────────────
async def set_state(artifact_id: str, state: dict, state_version: int, now: datetime) -> None:
    conn = connection()
    await conn.execute(
        "UPDATE artifacts SET state = ?, state_version = ?, rev = rev + 1, updated_at = ? WHERE id = ?",
        (json.dumps(state), state_version, now.isoformat(), artifact_id),
    )
    await conn.commit()


# ── documents ────────────────────────────────────────────────────────────────
async def get_doc(artifact_id: str, collection: str, doc_id: str) -> DocRow | None:
    async with connection().execute(
        "SELECT * FROM artifact_docs WHERE artifact_id = ? AND collection = ? AND doc_id = ?",
        (artifact_id, collection, doc_id),
    ) as cursor:
        row = await cursor.fetchone()
    return _doc_row(row) if row else None


async def list_docs(artifact_id: str, collection: str | None = None) -> list[DocRow]:
    sql = "SELECT * FROM artifact_docs WHERE artifact_id = ?"
    params: list = [artifact_id]
    if collection is not None:
        sql += " AND collection = ?"
        params.append(collection)
    async with connection().execute(sql + " ORDER BY collection, created_at, doc_id", params) as cursor:
        return [_doc_row(r) for r in await cursor.fetchall()]


async def count_docs(artifact_id: str, collection: str) -> int:
    async with connection().execute(
        "SELECT count(*) AS n FROM artifact_docs WHERE artifact_id = ? AND collection = ?",
        (artifact_id, collection),
    ) as cursor:
        return (await cursor.fetchone())["n"]


async def put_doc(artifact_id: str, row: DocRow, creating: bool, now: datetime, commit: bool = True) -> None:
    conn = connection()
    if creating:
        await conn.execute(
            "INSERT INTO artifact_docs (artifact_id, collection, doc_id, version, created_at, updated_at, data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (artifact_id, row.collection, row.id, row.version, row.created_at.isoformat(),
             row.updated_at.isoformat(), json.dumps(row.data)),
        )
    else:
        await conn.execute(
            "UPDATE artifact_docs SET version = ?, updated_at = ?, data = ? "
            "WHERE artifact_id = ? AND collection = ? AND doc_id = ?",
            (row.version, row.updated_at.isoformat(), json.dumps(row.data), artifact_id, row.collection, row.id),
        )
    await bump_rev(artifact_id, now, commit=commit)


async def delete_docs(artifact_id: str, collection: str, doc_id: str | None, now: datetime, commit: bool = True) -> int:
    conn = connection()
    sql = "DELETE FROM artifact_docs WHERE artifact_id = ? AND collection = ?"
    params: list = [artifact_id, collection]
    if doc_id is not None:
        sql += " AND doc_id = ?"
        params.append(doc_id)
    cursor = await conn.execute(sql, params)
    if cursor.rowcount:
        await bump_rev(artifact_id, now, commit=False)
    if commit:
        await conn.commit()
    return cursor.rowcount


async def bump_rev(artifact_id: str, now: datetime, commit: bool = True) -> None:
    conn = connection()
    await conn.execute(
        "UPDATE artifacts SET rev = rev + 1, updated_at = ? WHERE id = ?", (now.isoformat(), artifact_id)
    )
    if commit:
        await conn.commit()


async def commit() -> None:
    await connection().commit()


async def rollback() -> None:
    await connection().rollback()


# ── events ───────────────────────────────────────────────────────────────────
async def add_event(artifact_id: str, user_id: str, name: str, payload: dict, now: datetime) -> int:
    conn = connection()
    cursor = await conn.execute(
        "INSERT INTO artifact_events (artifact_id, user_id, name, created_at, payload) VALUES (?, ?, ?, ?, ?)",
        (artifact_id, user_id, name, now.isoformat(), json.dumps(payload)),
    )
    await conn.commit()
    return cursor.lastrowid


async def list_events(artifact_id: str, after: int, unhandled_only: bool, limit: int) -> list[dict]:
    sql = "SELECT seq, name, created_at, handled, payload FROM artifact_events WHERE artifact_id = ? AND seq > ?"
    if unhandled_only:
        sql += " AND handled = 0"
    async with connection().execute(sql + " ORDER BY seq LIMIT ?", (artifact_id, after, limit)) as cursor:
        rows = await cursor.fetchall()
    return [
        {"seq": r["seq"], "name": r["name"], "created_at": datetime.fromisoformat(r["created_at"]),
         "handled": bool(r["handled"]), "payload": json.loads(r["payload"])}
        for r in rows
    ]


async def mark_events_handled(artifact_id: str, upto: int) -> int:
    conn = connection()
    cursor = await conn.execute(
        "UPDATE artifact_events SET handled = 1 WHERE artifact_id = ? AND seq <= ? AND handled = 0",
        (artifact_id, upto),
    )
    await conn.commit()
    return cursor.rowcount
