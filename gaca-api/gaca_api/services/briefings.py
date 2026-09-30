"""Daily briefing business logic. No FastAPI, no HTTP.

Everything is scoped to the calling user. Writes are idempotent replaces, so an
agent's cron job can safely re-run.
"""
from __future__ import annotations

from datetime import date

from ..db import briefings as briefings_db
from ..models.base import utcnow
from ..models.briefing import BriefingDoc, BriefingItemDoc, BriefingSectionDoc
from ..schemas.briefing import BriefingItem, BriefingOut, SectionOut, SectionPut
from .exceptions import NotFound


def _section_out(s: BriefingSectionDoc) -> SectionOut:
    return SectionOut(
        key=s.key,
        title=s.title,
        kind=s.kind,
        headline=s.headline,
        items=[BriefingItem(**i.model_dump()) for i in s.items],
        display_order=s.display_order,
        updated_at=s.updated_at,
    )


async def get(user_id: str, day: date) -> BriefingOut:
    """The day's briefing: sections in display order (ties by key), plus summary.
    A day nobody posted to is an empty briefing, not a 404 — "nothing yet" is a
    normal state for the app to render."""
    briefing = await briefings_db.find_briefing(user_id, day)
    sections = sorted(
        await briefings_db.find_sections(user_id, day),
        key=lambda s: (s.display_order, s.key),
    )
    stamps = [s.updated_at for s in sections]
    if briefing is not None:
        stamps.append(briefing.updated_at)
    return BriefingOut(
        date=day,
        summary=briefing.summary if briefing else None,
        sections=[_section_out(s) for s in sections],
        updated_at=max(stamps) if stamps else None,
    )


async def set_summary(user_id: str, day: date, summary: str | None) -> BriefingOut:
    summary = summary.strip() if summary else None
    await briefings_db.upsert_briefing(
        BriefingDoc(user_id=user_id, date=day, summary=summary or None)
    )
    return await get(user_id, day)


async def put_section(
    user_id: str, day: date, key: str, body: SectionPut
) -> SectionOut:
    section = BriefingSectionDoc(
        user_id=user_id,
        date=day,
        key=key,
        title=body.title,
        kind=body.kind,
        headline=body.headline,
        items=[BriefingItemDoc(**i.model_dump()) for i in body.items],
        display_order=body.display_order,
        updated_at=utcnow(),
    )
    await briefings_db.upsert_section(section)
    return _section_out(section)


async def delete_section(user_id: str, day: date, key: str) -> None:
    if not await briefings_db.delete_section(user_id, day, key):
        raise NotFound("briefing section", f"{day.isoformat()}/{key}")


async def list_dates(user_id: str, limit: int = 60) -> list[date]:
    return await briefings_db.list_dates(user_id, limit)
