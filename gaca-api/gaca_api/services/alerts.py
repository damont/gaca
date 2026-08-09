"""Alert business logic. No FastAPI, no HTTP — just orchestration."""
from __future__ import annotations

from datetime import datetime

from ..db import alerts as alerts_db
from ..models.alert import AlertDoc
from ..models.base import utcnow
from ..schemas.alert import AlertCreate
from .exceptions import NotFound


def effective_status(alert: AlertDoc, now: datetime | None = None) -> str:
    """The status a client should display, which is not always the stored one.

    A snooze that has elapsed reads as `new` again. Deriving that on read is
    what lets the whole app run with no background worker: there is no sweeper
    flipping rows back on a timer, and nothing to fall behind or double-fire.
    The stored `status` stays `snoozed` — only the view changes.
    """
    if (
        alert.status == "snoozed"
        and alert.snooze_until is not None
        and alert.snooze_until <= (now or utcnow())
    ):
        return "new"
    return alert.status


async def create(user_id: str, payload: AlertCreate) -> tuple[AlertDoc, bool]:
    """Create an alert, or fold it onto the one it dedupes with.

    Returns (alert, created) so the route can answer 201 or 200.

    A re-fired condition resets the status to `new`. That is deliberate: if you
    dismissed "disk low" yesterday and the disk is still filling today, the
    alert should come back rather than stay silently dismissed. Ownership,
    identity and creation time are preserved — it is the same alert, updated.
    """
    if payload.dedupe_key:
        existing = await alerts_db.find_by_dedupe_key(user_id, payload.dedupe_key)
        if existing is not None:
            updated = AlertDoc.model_validate(
                {
                    **existing.model_dump(),
                    **payload.model_dump(),
                    "status": "new",
                    "status_changed_at": utcnow(),
                    # Clear anything the previous triage left behind.
                    "snooze_until": None,
                    "session_id": None,
                    "resolution_note": None,
                }
            )
            await alerts_db.replace(updated)
            return updated, False

    alert = AlertDoc(user_id=user_id, **payload.model_dump())
    await alerts_db.insert(alert)
    return alert, True


async def get(user_id: str, alert_id: str) -> AlertDoc:
    """One alert, if it belongs to this user.

    Someone else's alert raises NotFound rather than a forbidden error: a 403
    would confirm the id exists, which is information the caller has no business
    learning.
    """
    alert = await alerts_db.find(alert_id)
    if alert is None or alert.user_id != user_id:
        raise NotFound("alert", alert_id)
    return alert


async def list_for_user(
    user_id: str,
    *,
    statuses: list[str] | None = None,
    severities: list[str] | None = None,
    limit: int = 50,
    now: datetime | None = None,
) -> list[AlertDoc]:
    """A user's alerts, newest first, filtered on EFFECTIVE status.

    Filtering on effective status is the point: asking for `new` has to include
    alerts whose snooze has quietly run out, or a deferred alert would never
    come back.
    """
    now = now or utcnow()
    alerts = await alerts_db.find_by_user(user_id)

    if statuses:
        wanted = set(statuses)
        alerts = [a for a in alerts if effective_status(a, now) in wanted]
    if severities:
        wanted = set(severities)
        alerts = [a for a in alerts if a.severity in wanted]

    return alerts[:limit]
