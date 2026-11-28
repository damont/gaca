"""HTTP layer for token usage and the weekly efficiency review (docs/efficiency.md).

App only: no agent token scope reaches these. The numbers come from the engine's own records, read
off its box each time; nothing here is stored except the review's "done for this week" mark.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from ..models.user import UserDoc
from ..schemas.alert import AlertOut
from ..schemas.usage import UsageReport
from ..services import alerts as alerts_service
from ..services import efficiency_watch
from ..services import usage as service
from ..utils.auth import get_current_user

router = APIRouter(tags=["usage"])


@router.get("/usage", response_model=UsageReport)
async def usage(days: int = Query(7, ge=1, le=31), user: UserDoc = Depends(get_current_user)):
    """Model tokens the crew used in the last `days` days, per automation and per chat source,
    biggest first, with the window before for comparison, what it would cost on a pay-per-token
    provider, and the review's suggestions. A crew member whose usage can't be read is listed in
    `unavailable`."""
    return await service.report(user, days=days)


@router.post("/usage/review", response_model=AlertOut | None)
async def review_now(user: UserDoc = Depends(get_current_user)):
    """Run the weekly efficiency review now instead of waiting for its day. Posts (or refreshes)
    this week's alert and returns it; `null` when there is nothing worth suggesting."""
    alert = await efficiency_watch.review(user, force=True)
    return None if alert is None else (await alerts_service.to_out(user.id, [alert]))[0]
