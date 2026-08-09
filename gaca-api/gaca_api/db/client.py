"""SQLite connection — embedded SQLite is the system of record.

One connection for the whole process, opened in the app's lifespan and reused by
every request. SQLite is a single file (`settings.db_path`): no server, no port,
backups are a file copy. `connection()` hands callers the shared handle.

We enable WAL so reads (e.g. long-lived SSE streams) don't block the writer, and
use a Row factory so rows behave like dicts.

Storage shape: every table keeps indexed key columns for querying plus a `doc`
column holding the full Pydantic model as JSON. Objects stay natural — dump with
`model_dump_json()` to store, `model_validate_json()` to load.
"""
from __future__ import annotations

from pathlib import Path

import aiosqlite

from ..config import settings

_conn: aiosqlite.Connection | None = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id         TEXT PRIMARY KEY,
    email      TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    doc        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    id         TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    doc        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id         TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    doc        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_messages_session
    ON messages (session_id, created_at, id);
CREATE TABLE IF NOT EXISTS events (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    run_id     TEXT,
    type       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    doc        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_session
    ON events (session_id, seq);
"""


async def connect() -> None:
    """Open the shared connection and ensure the schema exists (startup).

    Creates the parent directory if needed so a configured path like
    /var/lib/gaca/gaca.db works on a fresh box.
    """
    global _conn
    Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
    _conn = await aiosqlite.connect(settings.db_path)
    _conn.row_factory = aiosqlite.Row
    await _conn.execute("PRAGMA journal_mode=WAL")
    await _conn.executescript(_SCHEMA)
    await _conn.commit()


async def close() -> None:
    """Close the shared connection on shutdown."""
    global _conn
    if _conn is not None:
        await _conn.close()
        _conn = None


def connection() -> aiosqlite.Connection:
    """The shared connection. Raises if used before the lifespan ran."""
    if _conn is None:
        raise RuntimeError("SQLite not initialized — is the lifespan running?")
    return _conn
