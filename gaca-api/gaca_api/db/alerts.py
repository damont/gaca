"""Data access for the `alerts` table. Returns DB models, not raw rows."""
from __future__ import annotations

from ..models.alert import AlertDoc
from .client import connection

_KEY_COLUMNS = "id, user_id, created_at, status, kind, severity, dedupe_key, doc"


def _values(alert: AlertDoc) -> tuple:
    return (
        alert.id,
        alert.user_id,
        alert.created_at.isoformat(),
        alert.status,
        alert.kind,
        alert.severity,
        alert.dedupe_key,
        alert.model_dump_json(),
    )


async def insert(alert: AlertDoc) -> None:
    conn = connection()
    await conn.execute(
        f"INSERT INTO alerts ({_KEY_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        _values(alert),
    )
    await conn.commit()


async def replace(alert: AlertDoc) -> None:
    """Overwrite an existing row in place, keeping its id.

    Used by the dedupe path: a re-fired condition updates the alert the user
    already has rather than pushing a second one at them.
    """
    conn = connection()
    await conn.execute(
        "UPDATE alerts SET user_id = ?, created_at = ?, status = ?, kind = ?, "
        "severity = ?, dedupe_key = ?, doc = ? WHERE id = ?",
        (*_values(alert)[1:], alert.id),
    )
    await conn.commit()


async def find(alert_id: str) -> AlertDoc | None:
    async with connection().execute(
        "SELECT doc FROM alerts WHERE id = ?", (alert_id,)
    ) as cursor:
        row = await cursor.fetchone()
    return AlertDoc.model_validate_json(row["doc"]) if row is not None else None


async def find_by_dedupe_key(user_id: str, dedupe_key: str) -> AlertDoc | None:
    async with connection().execute(
        "SELECT doc FROM alerts WHERE user_id = ? AND dedupe_key = ?",
        (user_id, dedupe_key),
    ) as cursor:
        row = await cursor.fetchone()
    return AlertDoc.model_validate_json(row["doc"]) if row is not None else None


async def find_by_user(user_id: str) -> list[AlertDoc]:
    """Every alert for a user, newest first.

    Status filtering happens in the service, not here: an alert's *effective*
    status depends on whether its snooze has elapsed, and keeping that rule in
    one place in Python beats duplicating it in SQL where the two can drift.
    Revisit if a single user's inbox ever gets large enough to matter.
    """
    async with connection().execute(
        "SELECT doc FROM alerts WHERE user_id = ? ORDER BY created_at DESC, id DESC",
        (user_id,),
    ) as cursor:
        rows = await cursor.fetchall()
    return [AlertDoc.model_validate_json(row["doc"]) for row in rows]
