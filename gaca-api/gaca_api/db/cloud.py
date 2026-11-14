"""Data access for the cloud link and the pointers to cloud gacabits. Plain SQL; the rules
live in services/cloud_*.py."""
from __future__ import annotations

import json
from datetime import datetime

from ..models.cloud import CloudLink, CloudPointer
from .client import connection

_LINK_COLS = (
    "user_id, cloud_url, status, scope, server_name, account_id, handle, server_id, token_enc, linked_at, "
    "pending_device_enc, pending_expires_at, pending_interval, pending_next_poll, created_at, agent_access, pending_user_code, pending_verification_uri"
)
_DT = ("linked_at", "pending_expires_at", "pending_next_poll", "created_at")


def _link(r) -> CloudLink:
    data = dict(r)
    for key in _DT:
        data[key] = datetime.fromisoformat(data[key]) if data[key] else None
    return CloudLink.model_validate(data)


async def get_link(user_id: str) -> CloudLink | None:
    async with connection().execute(f"SELECT {_LINK_COLS} FROM cloud_link WHERE user_id = ?", (user_id,)) as c:
        row = await c.fetchone()
    return _link(row) if row else None


async def save_link(link: CloudLink) -> None:
    conn = connection()
    values = link.model_dump()
    values["agent_access"] = int(link.agent_access)
    for key in _DT:
        values[key] = values[key].isoformat() if values[key] else None
    cols = _LINK_COLS.split(", ")
    await conn.execute(
        f"INSERT OR REPLACE INTO cloud_link ({_LINK_COLS}) VALUES ({', '.join('?' for _ in cols)})",
        [values[c] for c in cols],
    )
    await conn.commit()


async def delete_link(user_id: str) -> None:
    conn = connection()
    await conn.execute("DELETE FROM cloud_link WHERE user_id = ?", (user_id,))
    await conn.commit()


async def all_secrets() -> list[tuple[str, str | None, str | None]]:
    """(user_id, token_enc, pending_device_enc) of every link, for re-encrypting under a new key."""
    async with connection().execute("SELECT user_id, token_enc, pending_device_enc FROM cloud_link") as c:
        return [(r["user_id"], r["token_enc"], r["pending_device_enc"]) for r in await c.fetchall()]


async def set_secrets(user_id: str, token_enc: str | None, pending_device_enc: str | None) -> None:
    conn = connection()
    await conn.execute(
        "UPDATE cloud_link SET token_enc = ?, pending_device_enc = ? WHERE user_id = ?",
        (token_enc, pending_device_enc, user_id),
    )
    await conn.commit()


# ── pointers ─────────────────────────────────────────────────────────────────
_PTR_COLS = "id, user_id, cloud_id, account_id, role, shared_by, title, member_count, position, last_seen, meta"


def _pointer(r) -> CloudPointer:
    data = dict(r)
    data["last_seen"] = datetime.fromisoformat(data["last_seen"])
    data["meta"] = json.loads(data["meta"])
    return CloudPointer.model_validate(data)


async def get_pointer(user_id: str, pointer_id: str) -> CloudPointer | None:
    async with connection().execute(
        f"SELECT {_PTR_COLS} FROM cloud_pointers WHERE user_id = ? AND id = ?", (user_id, pointer_id)
    ) as c:
        row = await c.fetchone()
    return _pointer(row) if row else None


async def list_pointers(user_id: str) -> list[CloudPointer]:
    async with connection().execute(f"SELECT {_PTR_COLS} FROM cloud_pointers WHERE user_id = ?", (user_id,)) as c:
        return [_pointer(r) for r in await c.fetchall()]


async def upsert_pointer(p: CloudPointer, *, commit: bool = True) -> None:
    """Insert or refresh a pointer. The owner's manual `position` survives a refresh."""
    conn = connection()
    await conn.execute(
        "INSERT INTO cloud_pointers (id, user_id, cloud_id, account_id, role, shared_by, title, member_count, "
        "position, last_seen, meta) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (user_id, id) DO UPDATE SET cloud_id = excluded.cloud_id, account_id = excluded.account_id, "
        "role = excluded.role, shared_by = excluded.shared_by, title = excluded.title, "
        "member_count = excluded.member_count, last_seen = excluded.last_seen, meta = excluded.meta",
        (p.id, p.user_id, p.cloud_id, p.account_id, p.role, p.shared_by, p.title, p.member_count, p.position,
         p.last_seen.isoformat(), json.dumps(p.meta)),
    )
    if commit:
        await conn.commit()


async def delete_pointer(user_id: str, pointer_id: str, *, commit: bool = True) -> None:
    conn = connection()
    await conn.execute("DELETE FROM cloud_pointers WHERE user_id = ? AND id = ?", (user_id, pointer_id))
    if commit:
        await conn.commit()


async def commit() -> None:
    await connection().commit()


async def delete_pointers(user_id: str) -> None:
    conn = connection()
    await conn.execute("DELETE FROM cloud_pointers WHERE user_id = ?", (user_id,))
    await conn.commit()


async def set_positions(user_id: str, pairs: list[tuple[str, int]]) -> None:
    conn = connection()
    await conn.executemany(
        "UPDATE cloud_pointers SET position = ? WHERE id = ? AND user_id = ?",
        [(pos, pointer_id, user_id) for pointer_id, pos in pairs],
    )
    await conn.commit()
