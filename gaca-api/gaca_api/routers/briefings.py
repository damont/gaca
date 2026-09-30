"""HTTP layer for the daily briefing.

Routes are thin: validate, call the service, return. Every route requires auth
and is scoped to the caller. Agents (Hermes cron jobs) post sections here; the
app reads the assembled day.
"""
from __future__ import annotations

from datetime import date as Date

from fastapi import APIRouter, Depends, Path, Query

from ..models.user import UserDoc
from ..schemas.briefing import (
    SECTION_KEY_PATTERN,
    BriefingDates,
    BriefingOut,
    BriefingPut,
    SectionOut,
    SectionPut,
)
from ..services import briefings as briefing_service
from ..utils.auth import get_current_user

router = APIRouter(prefix="/briefings", tags=["briefings"])

_KEY = Path(pattern=SECTION_KEY_PATTERN, description="Section slug, e.g. 'calendar'")


@router.get("")
async def get_briefing(
    date: Date | None = Query(
        default=None, description="YYYY-MM-DD; defaults to today (server UTC date)"
    ),
    user: UserDoc = Depends(get_current_user),
) -> BriefingOut:
    """One day's briefing — empty (no sections) if nothing was posted yet.
    Clients should pass their own local date."""
    return await briefing_service.get(user.id, date or Date.today())


@router.get("/dates")
async def list_dates(
    user: UserDoc = Depends(get_current_user),
) -> BriefingDates:
    """Days that have a briefing, newest first (for browsing past days)."""
    return BriefingDates(dates=await briefing_service.list_dates(user.id))


@router.put("/{date}")
async def put_summary(
    date: Date, body: BriefingPut, user: UserDoc = Depends(get_current_user)
) -> BriefingOut:
    """Set (replace) the day's overall summary. Sections are unaffected."""
    return await briefing_service.set_summary(user.id, date, body.summary)


@router.put("/{date}/sections/{key}")
async def put_section(
    date: Date,
    body: SectionPut,
    key: str = _KEY,
    user: UserDoc = Depends(get_current_user),
) -> SectionOut:
    """Create or replace one section of the day's briefing. Idempotent: posting
    the same section again overwrites it, so a cron job can safely re-run."""
    return await briefing_service.put_section(user.id, date, key, body)


@router.delete("/{date}/sections/{key}", status_code=204)
async def delete_section(
    date: Date,
    key: str = _KEY,
    user: UserDoc = Depends(get_current_user),
) -> None:
    await briefing_service.delete_section(user.id, date, key)
