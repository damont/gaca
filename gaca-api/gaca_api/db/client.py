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

import json
from pathlib import Path

import aiosqlite

from ..config import settings

_conn: aiosqlite.Connection | None = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS server_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id         TEXT PRIMARY KEY,
    email      TEXT NOT NULL UNIQUE,
    google_sub TEXT,
    created_at TEXT NOT NULL,
    doc        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    id         TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
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
-- The daily briefing: one overall row per (user, day) plus one row per section.
-- Sections are keyed so each source (calendar, weather, news...) can replace
-- just its own section, idempotently, without touching the others.
-- Photos attached to chat messages. Uploaded first (message_id NULL), then claimed
-- by the message that references them. Bytes live in `data`; `doc` is metadata only.
CREATE TABLE IF NOT EXISTS attachments (
    id         TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    session_id TEXT NOT NULL,
    message_id TEXT,
    created_at TEXT NOT NULL,
    doc        TEXT NOT NULL,
    data       BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_attachments_session
    ON attachments (session_id, created_at);
-- Lists (shopping, gifts, to-dos...). Items are their own rows, not part of the list's
-- JSON, so two writers (the app and an agent) adding at once can't overwrite each other.
-- `name_key` / `item_key` are normalized forms (see services/lists.py:key); the unique
-- indexes make a duplicate list name or duplicate item impossible at the database level.
CREATE TABLE IF NOT EXISTS lists (
    id         TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    name_key   TEXT NOT NULL,
    archived   INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    doc        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_lists_user ON lists (user_id, archived, updated_at);
CREATE UNIQUE INDEX IF NOT EXISTS ux_lists_name ON lists (user_id, name_key) WHERE archived = 0;
CREATE TABLE IF NOT EXISTS list_items (
    id         TEXT PRIMARY KEY,
    list_id    TEXT NOT NULL,
    user_id    TEXT NOT NULL,
    item_key   TEXT NOT NULL,
    done       INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    doc        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_list_items_list ON list_items (list_id, done, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS ux_list_items_key ON list_items (list_id, item_key);
-- Artifacts: HTML pages an agent writes, plus the data they keep. The page (`html`) is a
-- stateless view; `state` is one JSON document (compare-and-set via `state_version`) and
-- artifact_docs holds keyed collections of small documents. `rev` increases on ANY data
-- write so a page can poll cheaply ("anything new since rev N?").
CREATE TABLE IF NOT EXISTS artifacts (
    id            TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL,
    archived      INTEGER NOT NULL DEFAULT 0,
    version       INTEGER NOT NULL DEFAULT 1,
    state         TEXT NOT NULL DEFAULT '{}',
    state_version INTEGER NOT NULL DEFAULT 0,
    rev           INTEGER NOT NULL DEFAULT 0,
    updated_at    TEXT NOT NULL,
    doc           TEXT NOT NULL,
    html          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_artifacts_user ON artifacts (user_id, archived, updated_at);
CREATE TABLE IF NOT EXISTS artifact_versions (
    artifact_id TEXT NOT NULL,
    version     INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    note        TEXT,
    html        TEXT NOT NULL,
    PRIMARY KEY (artifact_id, version)
);
CREATE TABLE IF NOT EXISTS artifact_docs (
    artifact_id TEXT NOT NULL,
    collection  TEXT NOT NULL,
    doc_id      TEXT NOT NULL,
    version     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    data        TEXT NOT NULL,
    PRIMARY KEY (artifact_id, collection, doc_id)
);
CREATE INDEX IF NOT EXISTS ix_artifact_docs_order
    ON artifact_docs (artifact_id, collection, created_at, doc_id);
-- What the page tells the agent happened ("checked out", "picked Friday"): a small
-- outbox the agent reads, so interactions flow back without a chat message.
CREATE TABLE IF NOT EXISTS artifact_events (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    artifact_id TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    handled     INTEGER NOT NULL DEFAULT 0,
    payload     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_artifact_events ON artifact_events (artifact_id, seq);
CREATE TABLE IF NOT EXISTS briefings (
    user_id       TEXT NOT NULL,
    briefing_date TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    doc           TEXT NOT NULL,
    PRIMARY KEY (user_id, briefing_date)
);
CREATE TABLE IF NOT EXISTS briefing_sections (
    user_id       TEXT NOT NULL,
    briefing_date TEXT NOT NULL,
    section_key   TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    doc           TEXT NOT NULL,
    PRIMARY KEY (user_id, briefing_date, section_key)
);
"""

# Columns added to a table AFTER it first shipped. `CREATE TABLE IF NOT EXISTS`
# does nothing at all when the table already exists, so a new column would never
# reach a database created before it — the schema would silently be right on a
# fresh machine and wrong on yours. SQLite has no `ADD COLUMN IF NOT EXISTS`,
# so we look before we leap.
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("users", "google_sub", "TEXT"),
    # Sessions predate multi-user auth entirely (the original chat feature
    # had no user concept at all) — `user_id` lived only inside `doc` until
    # GET /sessions (listing "my" sessions for the Chat tab) needed it as a
    # real, indexed column. Nullable here because ALTER TABLE can't backfill
    # a NOT NULL default from existing rows; _backfill_session_user_ids does
    # that from each row's own `doc` right after.
    ("sessions", "user_id", "TEXT"),
    # The owner's own drag-and-drop order of their artifacts; NULL = never placed (sorts first).
    ("artifacts", "position", "INTEGER"),
)

# Indexes that depend on a column from _ADDED_COLUMNS, so they can only be
# created after the migration above has run.
_LATE_INDEXES = """
CREATE UNIQUE INDEX IF NOT EXISTS ux_users_google_sub
    ON users (google_sub) WHERE google_sub IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_sessions_user
    ON sessions (user_id, created_at);
"""


async def _add_missing_columns(conn: aiosqlite.Connection) -> None:
    for table, column, ddl_type in _ADDED_COLUMNS:
        async with conn.execute(f"PRAGMA table_info({table})") as cursor:
            present = {row["name"] for row in await cursor.fetchall()}
        if column not in present:
            await conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")


async def _backfill_session_user_ids(conn: aiosqlite.Connection) -> None:
    """A session row added before the `user_id` column existed still has its
    real owner inside `doc` — promote it into the column so it's usable in a
    WHERE clause instead of requiring a full-table scan. No-op (empty
    result) on a database that never had the gap, fresh or otherwise.
    """
    async with conn.execute(
        "SELECT id, doc FROM sessions WHERE user_id IS NULL"
    ) as cursor:
        rows = await cursor.fetchall()
    for row in rows:
        owner = json.loads(row["doc"]).get("user_id")
        if owner is not None:
            await conn.execute(
                "UPDATE sessions SET user_id = ? WHERE id = ?", (owner, row["id"])
            )


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
    await _backfill_session_user_ids(_conn)
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
