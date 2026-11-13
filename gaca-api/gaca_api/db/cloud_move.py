"""Data access for moving artifacts to the cloud and back (Part 8b). Plain SQL; the rules live in
services/cloud_move.py.

The one hard requirement here: swapping an artifact's rows for a cloud pointer (and back) happens
in ONE transaction that no other request can interleave with. The server shares a single SQLite
connection between all requests, and any request's `commit()` would commit whatever another one had
half written, so the usual "execute, execute, commit" across several `await`s is NOT atomic here.
`_atomic` therefore hands a plain function to the connection's own worker thread, which runs it to
completion before looking at anything else.

A "snapshot" is the full local content of one artifact as a dict (see `read_snapshot`); it is what
a backup stores, what `restore_snapshot` writes back, and what a bundle from the cloud is turned
into when an artifact comes home.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
from datetime import datetime
from typing import Any, Callable

from ..models.cloud import CloudPointer
from .client import connection

SNAPSHOT_FORMAT = 1


async def _atomic(fn: Callable[[sqlite3.Connection], Any]) -> Any:
    """Run `fn(raw sqlite connection)` as one transaction on the connection's worker thread."""
    conn = connection()

    def run() -> Any:
        raw = conn._conn  # noqa: SLF001 - aiosqlite offers no public way to run several statements atomically
        if raw.in_transaction:
            raw.commit()  # somebody's finished-but-uncommitted write: it was going to be committed anyway
        raw.execute("BEGIN IMMEDIATE")
        try:
            out = fn(raw)
            raw.commit()
            return out
        except BaseException:
            raw.rollback()
            raise

    return await conn._execute(run)  # noqa: SLF001


# ── snapshots ────────────────────────────────────────────────────────────────
async def read_snapshot(user_id: str, artifact_id: str) -> dict[str, Any] | None:
    """Everything local about one artifact, or None. Read it while holding the artifact write lock."""
    conn = connection()
    async with conn.execute(
        "SELECT id, user_id, archived, version, state, state_version, rev, updated_at, position, doc, html "
        "FROM artifacts WHERE id = ? AND user_id = ?", (artifact_id, user_id)
    ) as c:
        row = await c.fetchone()
    if row is None:
        return None
    artifact = {
        "id": row["id"], "user_id": row["user_id"], "archived": bool(row["archived"]), "version": row["version"],
        "state": json.loads(row["state"]), "state_version": row["state_version"], "rev": row["rev"],
        "updated_at": row["updated_at"], "position": row["position"], "doc": json.loads(row["doc"]),
        "html": row["html"],
    }
    async with conn.execute(
        "SELECT version, created_at, note, html FROM artifact_versions WHERE artifact_id = ? ORDER BY version",
        (artifact_id,),
    ) as c:
        versions = [dict(r) for r in await c.fetchall()]
    async with conn.execute(
        "SELECT collection, doc_id, version, created_at, updated_at, data FROM artifact_docs "
        "WHERE artifact_id = ? ORDER BY collection, created_at, doc_id", (artifact_id,),
    ) as c:
        docs = [{**{k: r[k] for k in ("collection", "doc_id", "version", "created_at", "updated_at")},
                 "data": json.loads(r["data"])} for r in await c.fetchall()]
    async with conn.execute(
        "SELECT seq, name, created_at, handled, payload FROM artifact_events WHERE artifact_id = ? ORDER BY seq",
        (artifact_id,),
    ) as c:
        events = [{"seq": r["seq"], "name": r["name"], "created_at": r["created_at"], "handled": int(r["handled"]),
                   "payload": json.loads(r["payload"])} for r in await c.fetchall()]
    return {"format": SNAPSHOT_FORMAT, "artifact": artifact, "versions": versions, "docs": docs, "events": events}


def fingerprint(snapshot: dict[str, Any]) -> str:
    """A hash of the artifact's CONTENT: any write to it changes this; opening it or moving it in
    the list (last_opened_at, position) does not."""
    art = dict(snapshot["artifact"])
    art.pop("position", None)
    doc = dict(art["doc"])
    doc.pop("last_opened_at", None)
    art["doc"] = doc
    events = [{k: v for k, v in e.items() if k != "seq"} for e in snapshot["events"]]  # seq is assigned on insert
    blob = json.dumps({"a": art, "v": snapshot["versions"], "d": snapshot["docs"], "e": events},
                      sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()


def seal(snapshot: dict[str, Any]) -> tuple[bytes, str]:
    """(gzip bytes, sha256 of those bytes) of a snapshot."""
    data = gzip.compress(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode(), 6, mtime=0)
    return data, hashlib.sha256(data).hexdigest()


def _insert_snapshot(raw: sqlite3.Connection, snap: dict[str, Any], *, keep_event_seq: bool) -> None:
    a = snap["artifact"]
    raw.execute(
        "INSERT INTO artifacts (id, user_id, archived, version, state, state_version, rev, updated_at, doc, html, "
        "position) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (a["id"], a["user_id"], int(a["archived"]), a["version"], json.dumps(a["state"]), a["state_version"],
         a["rev"], a["updated_at"], json.dumps(a["doc"], separators=(",", ":")), a["html"], a.get("position")),
    )
    raw.executemany(
        "INSERT INTO artifact_versions (artifact_id, version, created_at, note, html) VALUES (?, ?, ?, ?, ?)",
        [(a["id"], v["version"], v["created_at"], v["note"], v["html"]) for v in snap["versions"]],
    )
    raw.executemany(
        "INSERT INTO artifact_docs (artifact_id, collection, doc_id, version, created_at, updated_at, data) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(a["id"], d["collection"], d["doc_id"], d["version"], d["created_at"], d["updated_at"], json.dumps(d["data"]))
         for d in snap["docs"]],
    )
    for e in snap["events"]:
        if keep_event_seq and e.get("seq") is not None:
            raw.execute(
                "INSERT INTO artifact_events (seq, artifact_id, user_id, name, created_at, handled, payload) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (e["seq"], a["id"], a["user_id"], e["name"], e["created_at"], int(e["handled"]), json.dumps(e["payload"])))
        else:
            raw.execute(
                "INSERT INTO artifact_events (artifact_id, user_id, name, created_at, handled, payload) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (a["id"], a["user_id"], e["name"], e["created_at"], int(e["handled"]), json.dumps(e["payload"])))


def _delete_local(raw: sqlite3.Connection, user_id: str, artifact_id: str) -> None:
    raw.execute("DELETE FROM artifacts WHERE id = ? AND user_id = ?", (artifact_id, user_id))
    for table in ("artifact_versions", "artifact_docs", "artifact_events"):
        raw.execute(f"DELETE FROM {table} WHERE artifact_id = ?", (artifact_id,))


def _upsert_pointer(raw: sqlite3.Connection, p: CloudPointer) -> None:
    raw.execute(
        "INSERT INTO cloud_pointers (id, user_id, cloud_id, account_id, role, shared_by, title, member_count, "
        "position, last_seen, meta) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (user_id, id) DO UPDATE SET cloud_id = excluded.cloud_id, account_id = excluded.account_id, "
        "role = excluded.role, shared_by = excluded.shared_by, title = excluded.title, "
        "member_count = excluded.member_count, position = excluded.position, last_seen = excluded.last_seen, "
        "meta = excluded.meta",
        (p.id, p.user_id, p.cloud_id, p.account_id, p.role, p.shared_by, p.title, p.member_count, p.position,
         p.last_seen.isoformat(), json.dumps(p.meta)),
    )


async def convert_to_pointer(
    snapshot: dict[str, Any], pointer: CloudPointer, *, backup_id: str, created_at: datetime, expires_at: datetime,
) -> None:
    """The local artifact becomes a cloud pointer, in ONE transaction: the sealed backup row is
    written first, then the local rows go, then the pointer and the bookkeeping rows change. Any
    failure rolls all of it back and the artifact is exactly as it was."""
    a = snapshot["artifact"]
    blob, digest = seal(snapshot)

    def work(raw: sqlite3.Connection) -> None:
        raw.execute(
            "INSERT INTO artifact_cloud_backups (id, artifact_id, user_id, snapshot, sha256, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (backup_id, a["id"], a["user_id"], blob, digest, created_at.isoformat(), expires_at.isoformat()))
        _delete_local(raw, a["user_id"], a["id"])
        _upsert_pointer(raw, pointer)
        raw.execute("DELETE FROM artifact_moves WHERE artifact_id = ?", (a["id"],))
        raw.execute("DELETE FROM artifact_bring_homes WHERE artifact_id = ?", (a["id"],))

    await _atomic(work)


async def restore_snapshot(
    snapshot: dict[str, Any], *, keep_event_seq: bool, bring_home: dict[str, Any] | None = None,
) -> None:
    """A cloud pointer (if there is one) becomes the local artifact again, in ONE transaction. Fails
    (and changes nothing) if a local artifact with this id already exists."""
    a = snapshot["artifact"]

    def work(raw: sqlite3.Connection) -> None:
        if raw.execute("SELECT 1 FROM artifacts WHERE id = ?", (a["id"],)).fetchone():
            raise FileExistsError(a["id"])
        raw.execute("DELETE FROM cloud_pointers WHERE user_id = ? AND id = ?", (a["user_id"], a["id"]))
        _insert_snapshot(raw, snapshot, keep_event_seq=keep_event_seq)
        raw.execute("DELETE FROM artifact_moves WHERE artifact_id = ?", (a["id"],))
        if bring_home is not None:
            raw.execute(
                "INSERT OR REPLACE INTO artifact_bring_homes (artifact_id, user_id, fingerprint, confirmed, undo_until, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (a["id"], a["user_id"], bring_home["fingerprint"], 0, bring_home.get("undo_until"),
                 bring_home["created_at"]))

    await _atomic(work)


# ── move failures (what the app shows) ───────────────────────────────────────
async def set_failure(user_id: str, artifact_id: str, phase: str, code: str, message: str, now: datetime) -> None:
    conn = connection()
    await conn.execute(
        "INSERT OR REPLACE INTO artifact_moves (artifact_id, user_id, phase, code, message, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)", (artifact_id, user_id, phase, code, message[:300], now.isoformat()))
    await conn.commit()


async def clear_failure(artifact_id: str) -> None:
    conn = connection()
    await conn.execute("DELETE FROM artifact_moves WHERE artifact_id = ?", (artifact_id,))
    await conn.commit()


async def failures(user_id: str) -> dict[str, dict[str, str]]:
    async with connection().execute(
        "SELECT artifact_id, phase, code, message FROM artifact_moves WHERE user_id = ?", (user_id,)
    ) as c:
        return {r["artifact_id"]: {"phase": r["phase"], "code": r["code"], "message": r["message"]}
                for r in await c.fetchall()}


# ── bring-home bookkeeping ───────────────────────────────────────────────────
async def get_bring_home(artifact_id: str) -> dict[str, Any] | None:
    async with connection().execute(
        "SELECT artifact_id, user_id, fingerprint, confirmed, undo_until, created_at FROM artifact_bring_homes "
        "WHERE artifact_id = ?", (artifact_id,)
    ) as c:
        row = await c.fetchone()
    return None if row is None else {**dict(row), "confirmed": bool(row["confirmed"])}


async def list_bring_homes(user_id: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT artifact_id, user_id, fingerprint, confirmed, undo_until, created_at FROM artifact_bring_homes"
    params: tuple = ()
    if user_id is not None:
        sql += " WHERE user_id = ?"
        params = (user_id,)
    async with connection().execute(sql, params) as c:
        return [{**dict(r), "confirmed": bool(r["confirmed"])} for r in await c.fetchall()]


async def mark_bring_home_confirmed(artifact_id: str, undo_until: str | None = None) -> None:
    conn = connection()
    await conn.execute(
        "UPDATE artifact_bring_homes SET confirmed = 1, undo_until = COALESCE(?, undo_until) WHERE artifact_id = ?",
        (undo_until, artifact_id))
    await conn.commit()


async def delete_bring_home(artifact_id: str) -> None:
    conn = connection()
    await conn.execute("DELETE FROM artifact_bring_homes WHERE artifact_id = ?", (artifact_id,))
    await conn.commit()


# ── backups ──────────────────────────────────────────────────────────────────
async def list_backups(user_id: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT id, artifact_id, user_id, created_at, expires_at, length(snapshot) AS size FROM artifact_cloud_backups"
    params: tuple = ()
    if user_id is not None:
        sql += " WHERE user_id = ?"
        params = (user_id,)
    async with connection().execute(sql + " ORDER BY created_at DESC", params) as c:
        return [dict(r) for r in await c.fetchall()]


async def find_backup(ref: str) -> dict[str, Any] | None:
    """A backup by its own id, or the newest one of an artifact id. Returns the row with the snapshot decoded
    and verified (sha256); a damaged one raises ValueError."""
    async with connection().execute(
        "SELECT id, artifact_id, user_id, snapshot, sha256, created_at, expires_at FROM artifact_cloud_backups "
        "WHERE id = ? OR artifact_id = ? ORDER BY created_at DESC LIMIT 1", (ref, ref)
    ) as c:
        row = await c.fetchone()
    if row is None:
        return None
    blob = bytes(row["snapshot"])
    if hashlib.sha256(blob).hexdigest() != row["sha256"]:
        raise ValueError("the stored backup does not match its checksum")
    snap = json.loads(gzip.decompress(blob))
    return {"id": row["id"], "artifact_id": row["artifact_id"], "user_id": row["user_id"],
            "created_at": row["created_at"], "expires_at": row["expires_at"], "snapshot": snap}


async def delete_expired_backups(now: datetime) -> int:
    conn = connection()
    cursor = await conn.execute("DELETE FROM artifact_cloud_backups WHERE expires_at <= ?", (now.isoformat(),))
    await conn.commit()
    return cursor.rowcount


async def has_backup(artifact_id: str) -> bool:
    async with connection().execute(
        "SELECT 1 FROM artifact_cloud_backups WHERE artifact_id = ? LIMIT 1", (artifact_id,)
    ) as c:
        return await c.fetchone() is not None
