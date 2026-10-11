"""Data access for `api_tokens`."""
from __future__ import annotations

import json
from datetime import datetime

from ..models.api_token import ApiTokenDoc
from .client import connection


def _doc(row) -> ApiTokenDoc:
    return ApiTokenDoc(
        id=row["id"], user_id=row["user_id"], name=row["name"], scopes=json.loads(row["scopes"]),
        created_at=row["created_at"], last_used_at=row["last_used_at"],
        expires_at=row["expires_at"], revoked_at=row["revoked_at"],
    )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


async def insert(doc: ApiTokenDoc, token_hash: str) -> None:
    conn = connection()
    await conn.execute(
        "INSERT INTO api_tokens (id, user_id, name, token_hash, scopes, created_at, last_used_at, expires_at, revoked_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (doc.id, doc.user_id, doc.name, token_hash, json.dumps(doc.scopes), doc.created_at.isoformat(),
         _iso(doc.last_used_at), _iso(doc.expires_at), _iso(doc.revoked_at)),
    )
    await conn.commit()


async def find_by_hash(token_hash: str) -> ApiTokenDoc | None:
    async with connection().execute("SELECT * FROM api_tokens WHERE token_hash = ?", (token_hash,)) as cursor:
        row = await cursor.fetchone()
    return _doc(row) if row else None


async def list_for_user(user_id: str) -> list[ApiTokenDoc]:
    async with connection().execute(
        "SELECT * FROM api_tokens WHERE user_id = ? ORDER BY created_at DESC", (user_id,)
    ) as cursor:
        return [_doc(row) async for row in cursor]


async def revoke(user_id: str, token_id: str, when: datetime) -> bool:
    """Mark one token revoked (idempotent). False if this user has no such token."""
    conn = connection()
    cursor = await conn.execute(
        "UPDATE api_tokens SET revoked_at = COALESCE(revoked_at, ?) WHERE id = ? AND user_id = ?",
        (when.isoformat(), token_id, user_id),
    )
    await conn.commit()
    return cursor.rowcount > 0


async def revoke_active_by_name(user_id: str, name: str, when: datetime, *, keep_id: str | None = None) -> int:
    conn = connection()
    cursor = await conn.execute(
        "UPDATE api_tokens SET revoked_at = ? WHERE user_id = ? AND name = ? AND revoked_at IS NULL AND id != ?",
        (when.isoformat(), user_id, name, keep_id or ""),
    )
    await conn.commit()
    return cursor.rowcount


async def touch(token_id: str, when: datetime) -> None:
    conn = connection()
    await conn.execute("UPDATE api_tokens SET last_used_at = ? WHERE id = ?", (when.isoformat(), token_id))
    await conn.commit()
