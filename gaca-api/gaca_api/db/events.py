"""Data access for the `events` table — the durable event log.

`seq` is an AUTOINCREMENT primary key: the insert assigns it atomically, which is
exactly the monotonic Last-Event-ID token the SSE layer needs (no per-session
counter to race on). The stored `doc` is the GacaEvent JSON; `seq` is
authoritative in the column and overlaid back onto the event on read.
"""
from __future__ import annotations

from ..schemas.event import GacaEvent, GacaEventAdapter
from .client import connection


async def insert(event: GacaEvent) -> int:
    """Persist an event; return its assigned seq (the autoincrement rowid)."""
    conn = connection()
    cursor = await conn.execute(
        "INSERT INTO events (session_id, run_id, type, created_at, doc) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            event.session_id,
            event.run_id,
            event.type,
            event.created_at.isoformat(),
            event.model_dump_json(),
        ),
    )
    await conn.commit()
    return cursor.lastrowid


async def find_by_session(session_id: str, after_seq: int = 0) -> list[GacaEvent]:
    """Events for a session with seq > after_seq, oldest first (resume replay)."""
    async with connection().execute(
        "SELECT seq, doc FROM events WHERE session_id = ? AND seq > ? ORDER BY seq",
        (session_id, after_seq),
    ) as cursor:
        rows = await cursor.fetchall()

    events: list[GacaEvent] = []
    for row in rows:
        event = GacaEventAdapter.validate_json(row["doc"])
        event.seq = row["seq"]  # the column is authoritative
        events.append(event)
    return events


async def find_by_run(run_id: str, types: tuple[str, ...]) -> list[GacaEvent]:
    """One run's events of the given types, oldest first — e.g. its tool calls,
    to summarize what the run did once it's over."""
    placeholders = ", ".join("?" for _ in types)
    async with connection().execute(
        f"SELECT seq, doc FROM events WHERE run_id = ? AND type IN ({placeholders}) ORDER BY seq",
        (run_id, *types),
    ) as cursor:
        rows = await cursor.fetchall()

    events: list[GacaEvent] = []
    for row in rows:
        event = GacaEventAdapter.validate_json(row["doc"])
        event.seq = row["seq"]
        events.append(event)
    return events


async def max_seq(session_id: str) -> int:
    """The session's latest seq, 0 if it has no events — "everything up to here"
    as a Last-Event-ID."""
    async with connection().execute(
        "SELECT MAX(seq) AS seq FROM events WHERE session_id = ?", (session_id,)
    ) as cursor:
        row = await cursor.fetchone()
    return row["seq"] or 0


async def min_seq_for_run(session_id: str, run_id: str) -> int | None:
    """The first seq a run persisted in this session, None if it hasn't persisted
    any yet. Uses the (session_id, seq) index; a session's runs are few."""
    async with connection().execute(
        "SELECT MIN(seq) AS seq FROM events WHERE session_id = ? AND run_id = ?",
        (session_id, run_id),
    ) as cursor:
        row = await cursor.fetchone()
    return row["seq"]
