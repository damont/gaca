"""Data access for the `messages` table."""
from __future__ import annotations

from datetime import datetime

from ..models.message import MessageDoc
from .client import connection


async def insert(message: MessageDoc) -> None:
    conn = connection()
    await conn.execute(
        "INSERT INTO messages (id, session_id, created_at, doc) VALUES (?, ?, ?, ?)",
        (
            message.id,
            message.session_id,
            message.created_at.isoformat(),
            message.model_dump_json(),
        ),
    )
    await conn.commit()


async def find_by_session(
    session_id: str, after: datetime | None = None
) -> list[MessageDoc]:
    """Messages for a session, oldest first.

    `after` is a cursor: when given, only messages created strictly after that
    timestamp are returned (used by the SSE poll loop to fetch just new ones).
    """
    sql = "SELECT doc FROM messages WHERE session_id = ?"
    params: list = [session_id]
    if after is not None:
        sql += " AND created_at > ?"
        params.append(after.isoformat())
    sql += " ORDER BY created_at, id"

    async with connection().execute(sql, params) as cursor:
        rows = await cursor.fetchall()
    return [MessageDoc.model_validate_json(row["doc"]) for row in rows]
