"""Data access for the `users` table. Returns DB models, not raw rows."""
from __future__ import annotations

from ..models.user import UserDoc
from .client import connection


async def insert(user: UserDoc) -> None:
    conn = connection()
    await conn.execute(
        "INSERT INTO users (id, email, created_at, doc) VALUES (?, ?, ?, ?)",
        (user.id, user.email, user.created_at.isoformat(), user.model_dump_json()),
    )
    await conn.commit()


async def find(user_id: str) -> UserDoc | None:
    async with connection().execute(
        "SELECT doc FROM users WHERE id = ?", (user_id,)
    ) as cursor:
        row = await cursor.fetchone()
    return UserDoc.model_validate_json(row["doc"]) if row is not None else None


async def find_by_email(email: str) -> UserDoc | None:
    async with connection().execute(
        "SELECT doc FROM users WHERE email = ?", (email,)
    ) as cursor:
        row = await cursor.fetchone()
    return UserDoc.model_validate_json(row["doc"]) if row is not None else None
