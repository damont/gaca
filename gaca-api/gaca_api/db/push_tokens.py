"""Data access for the `push_tokens` table."""
from __future__ import annotations

from ..models.push_token import PushTokenDoc
from .client import connection


async def upsert(push_token: PushTokenDoc) -> None:
    """Register a token, or move it to this user/platform if it already exists.

    Keyed on the token itself (unique), not (user_id, token) — an FCM token is
    device-specific, so if it shows up under a different user than before,
    that's the same device signed into a different account, and the token
    should follow it rather than fan out alerts to whoever registered first.
    """
    conn = connection()
    await conn.execute(
        """
        INSERT INTO push_tokens (id, user_id, token, created_at, doc, device_id)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(token) DO UPDATE SET
            id = excluded.id,
            user_id = excluded.user_id,
            created_at = excluded.created_at,
            doc = excluded.doc,
            device_id = excluded.device_id
        """,
        (
            push_token.id,
            push_token.user_id,
            push_token.token,
            push_token.created_at.isoformat(),
            push_token.model_dump_json(),
            push_token.device_id,
        ),
    )
    await conn.commit()


async def find_by_user(user_id: str) -> list[PushTokenDoc]:
    async with connection().execute(
        "SELECT doc FROM push_tokens WHERE user_id = ?", (user_id,)
    ) as cursor:
        rows = await cursor.fetchall()
    return [PushTokenDoc.model_validate_json(row["doc"]) for row in rows]


async def delete_by_token(token: str) -> None:
    """Drop a token — on sign-out, or when FCM reports it as unregistered/invalid."""
    conn = connection()
    await conn.execute("DELETE FROM push_tokens WHERE token = ?", (token,))
    await conn.commit()


async def delete_by_device(device_id: str) -> int:
    """Drop every token a paired device registered — when that device is unpaired or revoked."""
    conn = connection()
    cursor = await conn.execute("DELETE FROM push_tokens WHERE device_id = ?", (device_id,))
    await conn.commit()
    return cursor.rowcount
