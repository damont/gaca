"""Data access for the `reminders` table."""
from __future__ import annotations

from datetime import datetime

from ..models.reminder import ReminderDoc
from .client import connection


def _due(reminder: ReminderDoc) -> str | None:
    return reminder.due_at.isoformat() if reminder.due_at else None


async def insert(reminder: ReminderDoc) -> None:
    conn = connection()
    await conn.execute(
        "INSERT INTO reminders (id, user_id, state, due_at, doc) VALUES (?, ?, ?, ?, ?)",
        (reminder.id, reminder.user_id, reminder.state, _due(reminder), reminder.model_dump_json()),
    )
    await conn.commit()


async def replace(reminder: ReminderDoc) -> None:
    conn = connection()
    await conn.execute(
        "UPDATE reminders SET state = ?, due_at = ?, doc = ? WHERE id = ?",
        (reminder.state, _due(reminder), reminder.model_dump_json(), reminder.id),
    )
    await conn.commit()


async def find(reminder_id: str) -> ReminderDoc | None:
    async with connection().execute("SELECT doc FROM reminders WHERE id = ?", (reminder_id,)) as cursor:
        row = await cursor.fetchone()
    return ReminderDoc.model_validate_json(row["doc"]) if row else None


async def list_for_user(user_id: str, states: tuple[str, ...] | None = None) -> list[ReminderDoc]:
    """Soonest first; notes (no time) last, newest first among themselves."""
    sql = "SELECT doc FROM reminders WHERE user_id = ?"
    args: list = [user_id]
    if states:
        sql += f" AND state IN ({','.join('?' * len(states))})"
        args += list(states)
    sql += " ORDER BY due_at IS NULL, due_at ASC, id DESC"
    async with connection().execute(sql, args) as cursor:
        rows = await cursor.fetchall()
    return [ReminderDoc.model_validate_json(r["doc"]) for r in rows]


async def list_due(user_id: str, now: datetime) -> list[ReminderDoc]:
    async with connection().execute(
        "SELECT doc FROM reminders WHERE user_id = ? AND state = 'pending' "
        "AND due_at IS NOT NULL AND due_at <= ? ORDER BY due_at ASC, id ASC",
        (user_id, now.isoformat()),
    ) as cursor:
        rows = await cursor.fetchall()
    return [ReminderDoc.model_validate_json(r["doc"]) for r in rows]


async def claim(reminder: ReminderDoc, fired: ReminderDoc) -> bool:
    """Move `reminder` from pending to `fired` (its new document) in ONE conditional update, so two
    overlapping heartbeat runs cannot both fire it: only the one whose update changed a row wins."""
    conn = connection()
    cursor = await conn.execute(
        "UPDATE reminders SET state = ?, due_at = ?, doc = ? WHERE id = ? AND state = 'pending'",
        (fired.state, _due(fired), fired.model_dump_json(), reminder.id),
    )
    await conn.commit()
    return cursor.rowcount == 1


async def release(fired: ReminderDoc, original: ReminderDoc) -> None:
    """Undo a claim whose alert could not be made, so the reminder is tried again next tick."""
    conn = connection()
    await conn.execute(
        "UPDATE reminders SET state = ?, due_at = ?, doc = ? WHERE id = ? AND state = ?",
        (original.state, _due(original), original.model_dump_json(), original.id, fired.state),
    )
    await conn.commit()
