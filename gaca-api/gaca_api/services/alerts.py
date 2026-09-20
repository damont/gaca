"""Alert business logic. No FastAPI, no HTTP — just orchestration."""
from __future__ import annotations

from datetime import datetime

from ..db import alerts as alerts_db
from ..models.alert import AlertDoc
from ..models.base import utcnow
from ..models.user import UserDoc
from ..schemas.alert import AlertCreate
from ..schemas.event import GacaEvent, RunCompleted, RunFailed
from . import auth as auth_service
from . import push as push_service
from . import runs as run_service
from . import sessions as session_service
from .exceptions import InvalidRequest, InvalidTransition, NotFound

# Manual triage is only possible out of the inbox states. `delegated` is settled
# by the run working on it, not by hand; `acted` and `dismissed` are terminal.
# `completed` — Hermes finished, but not yet closed out — is triageable the
# same as `new`.
TRIAGEABLE = ("new", "snoozed", "completed")


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
            await push_service.notify(
                user_id,
                title=updated.title,
                body=updated.body or "",
                alert_id=updated.id,
                severity=updated.severity,
            )
            return updated, False

    alert = AlertDoc(user_id=user_id, **payload.model_dump())
    await alerts_db.insert(alert)
    await push_service.notify(
        user_id,
        title=alert.title,
        body=alert.body or "",
        alert_id=alert.id,
        severity=alert.severity,
    )
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


async def _resolution_note_for(user_id: str, alert_id: str, note: str | None) -> str | None:
    """The note to store: the caller's, if given — otherwise whatever's
    already there.

    Matters most for a `completed` alert: it arrives with `resolution_note`
    set to Hermes's own output, and closing it out with no note of your own
    should keep that, not silently wipe it with None.
    """
    if note is not None:
        return note
    return (await get(user_id, alert_id)).resolution_note


async def act(user_id: str, alert_id: str, note: str | None = None) -> AlertDoc:
    """Mark an alert handled. Terminal.

    `snooze_until` is cleared on the way out so no stale deferral is left
    hanging on a settled alert.
    """
    resolution_note = await _resolution_note_for(user_id, alert_id, note)
    return await _triage(
        user_id, alert_id, "acted", resolution_note=resolution_note, snooze_until=None
    )


async def dismiss(user_id: str, alert_id: str, note: str | None = None) -> AlertDoc:
    """Ignore an alert. Terminal — but a re-fired dedupe_key still resurfaces it."""
    resolution_note = await _resolution_note_for(user_id, alert_id, note)
    return await _triage(
        user_id, alert_id, "dismissed", resolution_note=resolution_note, snooze_until=None
    )


async def snooze(user_id: str, alert_id: str, until: datetime) -> AlertDoc:
    """Defer an alert until `until`, after which it reads as `new` again.

    Nothing is scheduled — no timer, no worker, no row to sweep. The deferral is
    a stored timestamp and `effective_status` does the rest on the next read.
    """
    return await _triage(user_id, alert_id, "snoozed", snooze_until=until)


def _delegate_prompt(
    alert: AlertDoc, instruction: str | None, suggested_action_index: int | None
) -> str:
    """Build the text handed to the agent for a delegated alert.

    A chosen `suggested_action` seeds the prompt with its proven text; a
    free-text `instruction` layers on top of that, or stands alone with no
    action chosen. With neither, fall back to the alert's own title/body so
    the agent still has something to work from.
    """
    parts: list[str] = []
    if suggested_action_index is not None:
        actions = alert.suggested_actions
        if not (0 <= suggested_action_index < len(actions)):
            raise InvalidRequest(
                f"suggested_action_index {suggested_action_index} is out of "
                f"range for alert {alert.id!r} ({len(actions)} action(s))"
            )
        parts.append(actions[suggested_action_index].prompt)
    if instruction:
        parts.append(instruction)
    if not parts:
        parts.append(f"{alert.title}\n\n{alert.body}" if alert.body else alert.title)
    return "\n\n".join(parts)


async def delegate(
    user: UserDoc,
    alert_id: str,
    *,
    instruction: str | None = None,
    suggested_action_index: int | None = None,
    crew: str | None = None,
) -> AlertDoc:
    """Hand an alert to one of the caller's own crew members: open a session,
    start a run, mark it delegated.

    Which crew member handles it is resolved by
    auth_service.resolve_crew_for_delegate: `crew` (an explicit, named
    choice) first, then the alert's own `crew` field (who raised it), then
    the account's default. Takes the full UserDoc, not just an id — resolving
    a crew member needs the user's configured host/crew (raises
    CrewNotConfigured if none resolve, same as the chat routes).

    The session this opens carries `crew` (which member owns it, fixed for
    its lifetime) and `alert_id` (the chat-side half of the bidirectional
    link back to this alert — the alert holds the forward pointer via
    `session_id`, set below).

    Same guard as manual triage — only a TRIAGEABLE alert (`new`, `snoozed`,
    or `completed`) can be delegated. The run settles the alert asynchronously
    via `_on_run_settled`, wired in as `on_settled` below; this call only
    starts the run, it doesn't wait for it.
    """
    alert = await get(user.id, alert_id)
    if alert.status not in TRIAGEABLE:
        raise InvalidTransition("alert", alert_id, alert.status, "delegated")

    # Validate the request against the alert's own data before touching the
    # crew at all — a bad suggested_action_index is a 422 regardless of
    # whether this user has even configured a crew member yet.
    prompt = _delegate_prompt(alert, instruction, suggested_action_index)
    crew_member = auth_service.resolve_crew_for_delegate(user, crew, alert.crew)
    backend = auth_service.backend_for_crew(user, crew_member)
    session = await session_service.create(
        user.id, alert.title, crew_member.name, alert_id=alert.id
    )

    updated = alert.model_copy(
        update={
            "status": "delegated",
            "status_changed_at": utcnow(),
            "session_id": session.id,
            "snooze_until": None,
        }
    )
    await alerts_db.replace(updated)

    async def _on_settled(event: GacaEvent) -> None:
        await _settle_delegation(alert_id, event)

    await run_service.start_turn(backend, session.id, prompt, on_settled=_on_settled)
    return updated


async def _settle_delegation(alert_id: str, event: GacaEvent) -> None:
    """Resolve a delegated alert once its run ends, one way or the other.

    A completed run means Hermes finished the work: settle to `completed`,
    not `acted` — finishing isn't the same as being closed out, and
    `resolution_note` gets the run's own output so there's something to
    actually look at. Only a later `act`/`dismiss` (a real user action)
    reaches a terminal state from here. A failed run bounces back to `new`
    with the error in `resolution_note`, so a failed delegation can never
    silently vanish — it's the same rule slice 4 applies to every other
    terminal outcome, just triggered by the agent instead of a hand.

    Reads the row fresh rather than trusting the copy captured at delegate()
    time, and no-ops if it's no longer `delegated` — the alert could have moved
    between then and now (a second settlement racing this one, in principle),
    and this must not clobber whatever state it's actually in.
    """
    current = await alerts_db.find(alert_id)
    if current is None or current.status != "delegated":
        return

    if isinstance(event, RunCompleted):
        resolved = current.model_copy(
            update={
                "status": "completed",
                "status_changed_at": utcnow(),
                "resolution_note": event.output,
            }
        )
        await alerts_db.replace(resolved)
        await push_service.notify(
            resolved.user_id,
            title=f"Hermes finished: {resolved.title}",
            body="Tap to see what it found.",
            alert_id=resolved.id,
            severity=resolved.severity,
        )
    elif isinstance(event, RunFailed):
        resolved = current.model_copy(
            update={
                "status": "new",
                "status_changed_at": utcnow(),
                "resolution_note": event.error,
            }
        )
        await alerts_db.replace(resolved)
        await push_service.notify(
            resolved.user_id,
            title=f"Hermes couldn't finish: {resolved.title}",
            body=event.error or "The delegated run failed — it's back in your inbox.",
            alert_id=resolved.id,
            severity=resolved.severity,
        )
    else:
        return


async def list_for_user(
    user_id: str,
    *,
    statuses: list[str] | None = None,
    severities: list[str] | None = None,
    crews: list[str] | None = None,
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
    if crews:
        wanted = set(crews)
        alerts = [a for a in alerts if a.crew in wanted]

    return alerts[:limit]
