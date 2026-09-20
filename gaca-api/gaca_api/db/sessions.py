"""Data access for the `sessions` table. Returns DB models, not raw rows."""
from __future__ import annotations

from ..models.session import SessionDoc
from .client import connection


async def insert(session: SessionDoc) -> None:
    conn = connection()
    await conn.execute(
        "INSERT INTO sessions (id, user_id, created_at, doc) VALUES (?, ?, ?, ?)",
        (
            session.id,
            session.user_id,
            session.created_at.isoformat(),
            session.model_dump_json(),
        ),
    )
    await conn.commit()


async def find(session_id: str) -> SessionDoc | None:
    async with connection().execute(
        "SELECT doc FROM sessions WHERE id = ?", (session_id,)
    ) as cursor:
        row = await cursor.fetchone()
    return SessionDoc.model_validate_json(row["doc"]) if row is not None else None


async def exists(session_id: str) -> bool:
    async with connection().execute(
        "SELECT 1 FROM sessions WHERE id = ? LIMIT 1", (session_id,)
    ) as cursor:
        return await cursor.fetchone() is not None


async def find_by_user(user_id: str) -> list[SessionDoc]:
    """Every session for a user, most recently ACTIVE first — the Chat tab's
    list. "Active" means the newest message in it, not when the session was
    created, so a months-old session that just got a reply jumps back to
    the top; one with no messages yet (created but never sent to) sorts by
    its own `created_at` instead, since there's no message time to use.
    """
    async with connection().execute(
        """
        SELECT s.doc
        FROM sessions s
        LEFT JOIN (
            SELECT session_id, MAX(created_at) AS last_activity
            FROM messages
            GROUP BY session_id
        ) m ON m.session_id = s.id
        WHERE s.user_id = ?
        ORDER BY COALESCE(m.last_activity, s.created_at) DESC, s.id DESC
        """,
        (user_id,),
    ) as cursor:
        rows = await cursor.fetchall()
    return [SessionDoc.model_validate_json(row["doc"]) for row in rows]
