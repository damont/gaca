"""Data access for `server_meta`: small server-level facts that belong to no user."""
from __future__ import annotations

import json

from .client import connection


async def get(key: str) -> dict | None:
    async with connection().execute("SELECT value FROM server_meta WHERE key = ?", (key,)) as cursor:
        row = await cursor.fetchone()
    return json.loads(row["value"]) if row is not None else None


async def put(key: str, value: dict) -> None:
    conn = connection()
    await conn.execute(
        "INSERT INTO server_meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value)),
    )
    await conn.commit()


async def delete(key: str) -> None:
    conn = connection()
    await conn.execute("DELETE FROM server_meta WHERE key = ?", (key,))
    await conn.commit()
