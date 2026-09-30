"""Data access for the `briefings` and `briefing_sections` tables."""
from __future__ import annotations

from datetime import date

from ..models.briefing import BriefingDoc, BriefingSectionDoc
from .client import connection


async def upsert_briefing(briefing: BriefingDoc) -> None:
    conn = connection()
    await conn.execute(
        """
        INSERT INTO briefings (user_id, briefing_date, updated_at, doc)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id, briefing_date) DO UPDATE SET
            updated_at = excluded.updated_at, doc = excluded.doc
        """,
        (
            briefing.user_id,
            briefing.date.isoformat(),
            briefing.updated_at.isoformat(),
            briefing.model_dump_json(),
        ),
    )
    await conn.commit()


async def find_briefing(user_id: str, day: date) -> BriefingDoc | None:
    async with connection().execute(
        "SELECT doc FROM briefings WHERE user_id = ? AND briefing_date = ?",
        (user_id, day.isoformat()),
    ) as cursor:
        row = await cursor.fetchone()
    return BriefingDoc.model_validate_json(row["doc"]) if row else None


async def upsert_section(section: BriefingSectionDoc) -> None:
    conn = connection()
    await conn.execute(
        """
        INSERT INTO briefing_sections
            (user_id, briefing_date, section_key, updated_at, doc)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(user_id, briefing_date, section_key) DO UPDATE SET
            updated_at = excluded.updated_at, doc = excluded.doc
        """,
        (
            section.user_id,
            section.date.isoformat(),
            section.key,
            section.updated_at.isoformat(),
            section.model_dump_json(),
        ),
    )
    await conn.commit()


async def find_sections(user_id: str, day: date) -> list[BriefingSectionDoc]:
    async with connection().execute(
        "SELECT doc FROM briefing_sections WHERE user_id = ? AND briefing_date = ?",
        (user_id, day.isoformat()),
    ) as cursor:
        rows = await cursor.fetchall()
    return [BriefingSectionDoc.model_validate_json(r["doc"]) for r in rows]


async def delete_section(user_id: str, day: date, key: str) -> bool:
    conn = connection()
    cursor = await conn.execute(
        "DELETE FROM briefing_sections "
        "WHERE user_id = ? AND briefing_date = ? AND section_key = ?",
        (user_id, day.isoformat(), key),
    )
    await conn.commit()
    return cursor.rowcount > 0


async def list_dates(user_id: str, limit: int) -> list[date]:
    """Days that have any content (a summary or a section), newest first."""
    async with connection().execute(
        """
        SELECT briefing_date FROM (
            SELECT briefing_date FROM briefings WHERE user_id = ?
            UNION
            SELECT briefing_date FROM briefing_sections WHERE user_id = ?
        ) ORDER BY briefing_date DESC LIMIT ?
        """,
        (user_id, user_id, limit),
    ) as cursor:
        rows = await cursor.fetchall()
    return [date.fromisoformat(r["briefing_date"]) for r in rows]
