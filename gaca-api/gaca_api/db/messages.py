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


async def find_edges_by_user(
    user_id: str,
) -> dict[str, tuple[MessageDoc | None, MessageDoc | None]]:
    """For each of a user's sessions: (first user message, latest message).

    One query for the whole Chat list — what the list needs to label a session
    without loading every transcript. Sessions with no messages map to
    (None, None).
    """
    async with connection().execute(
        """
        SELECT s.id AS sid,
          (SELECT doc FROM messages
            WHERE session_id = s.id AND json_extract(doc, '$.role') = 'user'
            ORDER BY created_at, id LIMIT 1) AS first_user,
          (SELECT doc FROM messages
            WHERE session_id = s.id
            ORDER BY created_at DESC, id DESC LIMIT 1) AS last
        FROM sessions s
        WHERE s.user_id = ?
        """,
        (user_id,),
    ) as cursor:
        rows = await cursor.fetchall()

    def load(doc: str | None) -> MessageDoc | None:
        return MessageDoc.model_validate_json(doc) if doc else None

    return {r["sid"]: (load(r["first_user"]), load(r["last"])) for r in rows}
