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
    """Every session for a user, newest first — the Chat tab's list."""
    async with connection().execute(
        "SELECT doc FROM sessions WHERE user_id = ? ORDER BY created_at DESC, id DESC",
        (user_id,),
    ) as cursor:
        rows = await cursor.fetchall()
    return [SessionDoc.model_validate_json(row["doc"]) for row in rows]
