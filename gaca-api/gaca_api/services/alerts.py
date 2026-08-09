"""Alert business logic. No FastAPI, no HTTP — just orchestration."""
from __future__ import annotations

from datetime import datetime

from ..db import alerts as alerts_db
from ..models.alert import AlertDoc
from ..models.base import utcnow
from ..schemas.alert import AlertCreate
from .exceptions import InvalidTransition, NotFound

# Manual triage is only possible out of the inbox states. `delegated` is settled
# by the run working on it, not by hand; `acted` and `dismissed` are terminal.
TRIAGEABLE = ("new", "snoozed")


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

    THE UPDATE IS A FULL REPLACE OF THE CONTENT FIELDS, NOT A MERGE. Anything
    the caller omits reverts to its default — omit `suggested_actions` and the
    old ones are cleared, omit `source` and it goes back to "unknown". This is
    intentional, not an oversight:

      * An alert describes a condition as it stands *now*. A `suggested_action`
        carries a `prompt` that gets handed to an agent to execute; a stale one
        surviving a re-post means tapping a plausible-looking button dispatches
        work based on an hour-old picture. Losing buttons is a visible,
        harmless failure; keeping wrong ones is neither.
      * It makes the endpoint genuinely idempotent — the same body twice gives
        the same row regardless of history, which is what you want from
        something a five-minute cron hits.

    The cost is that a terse re-post silently strips detail, and the poster is
    an LLM that may not resend unchanged fields. That is a judgment rule rather
    than a schema rule, so the Hermes skill has to say "a re-post is a complete
    restatement, not a delta". If we ever want merge semantics instead, it is
    `payload.model_dump(exclude_unset=True)` here — but then the result depends
    on history, which makes a noisy poster much harder to debug.
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


async def _triage(
    user_id: str, alert_id: str, to_status: str, **fields
) -> AlertDoc:
    """Move an alert into `to_status`, or refuse.

    The guard reads the STORED status, not the effective one. They only ever
    differ for an elapsed snooze, and `snoozed` is triageable anyway — so the
    two agree here, and using the stored value keeps the rule about the real
    state rather than a derived view of it.
    """
    alert = await get(user_id, alert_id)
    if alert.status not in TRIAGEABLE:
        raise InvalidTransition("alert", alert_id, alert.status, to_status)

    updated = alert.model_copy(
        update={"status": to_status, "status_changed_at": utcnow(), **fields}
    )
    await alerts_db.replace(updated)
    return updated


async def act(user_id: str, alert_id: str, note: str | None = None) -> AlertDoc:
    """Mark an alert handled. Terminal.

    `snooze_until` is cleared on the way out so no stale deferral is left
    hanging on a settled alert.
    """
    return await _triage(
        user_id, alert_id, "acted", resolution_note=note, snooze_until=None
    )


async def dismiss(user_id: str, alert_id: str, note: str | None = None) -> AlertDoc:
    """Ignore an alert. Terminal — but a re-fired dedupe_key still resurfaces it."""
    return await _triage(
        user_id, alert_id, "dismissed", resolution_note=note, snooze_until=None
    )


async def snooze(user_id: str, alert_id: str, until: datetime) -> AlertDoc:
    """Defer an alert until `until`, after which it reads as `new` again.

    Nothing is scheduled — no timer, no worker, no row to sweep. The deferral is
    a stored timestamp and `effective_status` does the rest on the next read.
    """
    return await _triage(user_id, alert_id, "snoozed", snooze_until=until)


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
