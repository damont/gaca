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
    google_sub TEXT,
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
CREATE TABLE IF NOT EXISTS alerts (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    status      TEXT NOT NULL,
    kind        TEXT NOT NULL,
    severity    TEXT NOT NULL,
    dedupe_key  TEXT,
    doc         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_alerts_inbox
    ON alerts (user_id, status, created_at);
-- Partial unique index: dedupe applies per user, and only to alerts that
-- actually carry a key. Without the WHERE clause every keyless alert would
-- collide on NULL in some engines; with it, the database — not the route's
-- check-then-insert — is what makes a duplicate impossible.
CREATE UNIQUE INDEX IF NOT EXISTS ux_alerts_dedupe
    ON alerts (user_id, dedupe_key) WHERE dedupe_key IS NOT NULL;
CREATE TABLE IF NOT EXISTS push_tokens (
    id         TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    token      TEXT NOT NULL,
    created_at TEXT NOT NULL,
    doc        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_push_tokens_user
    ON push_tokens (user_id);
-- One row per device token, full stop: a token that shows up again (same
-- device re-registering, or moving to a different account) upserts in place
-- rather than accumulating duplicates that would each get their own push.
CREATE UNIQUE INDEX IF NOT EXISTS ux_push_tokens_token
    ON push_tokens (token);
"""

# Columns added to a table AFTER it first shipped. `CREATE TABLE IF NOT EXISTS`
# does nothing at all when the table already exists, so a new column would never
# reach a database created before it — the schema would silently be right on a
# fresh machine and wrong on yours. SQLite has no `ADD COLUMN IF NOT EXISTS`,
# so we look before we leap.
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("users", "google_sub", "TEXT"),
)

# Indexes that depend on a column from _ADDED_COLUMNS, so they can only be
# created after the migration above has run.
_LATE_INDEXES = """
CREATE UNIQUE INDEX IF NOT EXISTS ux_users_google_sub
    ON users (google_sub) WHERE google_sub IS NOT NULL;
"""


async def _add_missing_columns(conn: aiosqlite.Connection) -> None:
    for table, column, ddl_type in _ADDED_COLUMNS:
        async with conn.execute(f"PRAGMA table_info({table})") as cursor:
            present = {row["name"] for row in await cursor.fetchall()}
        if column not in present:
            await conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")


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
    await _add_missing_columns(_conn)
    await _conn.executescript(_LATE_INDEXES)
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
