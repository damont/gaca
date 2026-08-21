"""Data access for the `users` table. Returns DB models, not raw rows."""
from __future__ import annotations

from ..models.user import UserDoc
from .client import connection


async def insert(user: UserDoc) -> None:
    conn = connection()
    await conn.execute(
        "INSERT INTO users (id, email, google_sub, created_at, doc) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            user.id,
            user.email,
            user.google_sub,
            user.created_at.isoformat(),
            user.model_dump_json(),
        ),
    )
    await conn.commit()


async def replace(user: UserDoc) -> None:
    """Overwrite an existing row in place, keeping its id.

    Used when linking a Google account onto a user who already signed up with a
    password — same person, same row, now reachable both ways.
    """
    conn = connection()
    await conn.execute(
        "UPDATE users SET email = ?, google_sub = ?, doc = ? WHERE id = ?",
        (user.email, user.google_sub, user.model_dump_json(), user.id),
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


async def find_by_google_sub(google_sub: str) -> UserDoc | None:
    """Look a user up by Google's stable subject id.

    Keyed on `sub` rather than email because Google lets a user change their
    address; the sub never changes.
    """
    async with connection().execute(
        "SELECT doc FROM users WHERE google_sub = ?", (google_sub,)
    ) as cursor:
        row = await cursor.fetchone()
    return UserDoc.model_validate_json(row["doc"]) if row is not None else None
