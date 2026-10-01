"""Data access for the `attachments` table — metadata in `doc`, bytes in `data`."""
from __future__ import annotations

from datetime import datetime

from ..models.attachment import AttachmentDoc
from .client import connection


async def insert(doc: AttachmentDoc, data: bytes) -> None:
    conn = connection()
    await conn.execute(
        "INSERT INTO attachments (id, user_id, session_id, message_id, created_at, doc, data) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (doc.id, doc.user_id, doc.session_id, doc.message_id,
         doc.created_at.isoformat(), doc.model_dump_json(), data),
    )
    await conn.commit()


async def find(attachment_id: str) -> tuple[AttachmentDoc, bytes] | None:
    async with connection().execute(
        "SELECT doc, data, message_id FROM attachments WHERE id = ?", (attachment_id,)
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    doc = AttachmentDoc.model_validate_json(row["doc"])
    doc.message_id = row["message_id"]  # the column is the truth once claimed
    return doc, bytes(row["data"])


async def attach(attachment_ids: list[str], message_id: str) -> None:
    """Claim uploaded photos for a message."""
    conn = connection()
    for attachment_id in attachment_ids:
        await conn.execute(
            "UPDATE attachments SET message_id = ? WHERE id = ? AND message_id IS NULL",
            (message_id, attachment_id),
        )
    await conn.commit()


async def delete_orphans(user_id: str, before: datetime) -> None:
    """Drop this user's uploads that never made it into a message."""
    conn = connection()
    await conn.execute(
        "DELETE FROM attachments WHERE user_id = ? AND message_id IS NULL AND created_at < ?",
        (user_id, before.isoformat()),
    )
    await conn.commit()
