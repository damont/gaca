"""Reminder business logic. No FastAPI, no HTTP. See docs/reminders.md.

A reminder with a time fires as an ordinary alert (same inbox, same push). One without is a "later" note.
The heartbeat drives delivery by calling `fire_due`; nothing here runs on its own clock.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo

from ..db import alerts as alerts_db
from ..db import reminders as reminders_db
from ..models.base import utcnow
from ..models.heartbeat import HeartbeatSettings
from ..models.reminder import LATE_AFTER, NOTE_LIFETIME, OPEN_STATES, ReminderDoc, ReminderSource, to_utc_seconds
from ..models.user import UserDoc
from ..schemas.alert import AlertCreate
from ..schemas.reminder import FireDueOut, ReminderCreate, ReminderOut, ReminderPreset, ReminderUpdate
from . import alerts as alert_service
from .exceptions import InvalidRequest, InvalidTransition, NotFound

# An agent that gets the date wrong should hear about it, not silently fire a reminder at once or never.
PAST_GRACE = timedelta(hours=1)
MAX_AHEAD = timedelta(days=366 * 2)
# Two pending reminders with the same words and times this close are the same reminder.
DUPLICATE_WINDOW = timedelta(minutes=30)
MAX_PENDING = 200


def _zone(settings: HeartbeatSettings) -> tzinfo:
    """The person's zone, or the machine's own when they have not set one (as the heartbeat does)."""
    if settings.timezone:
        return ZoneInfo(settings.timezone)
    return datetime.now().astimezone().tzinfo or timezone.utc


def in_quiet_hours(settings: HeartbeatSettings, now: datetime) -> bool:
    start, end = settings.quiet_start_hour, settings.quiet_end_hour
    if start == end:
        return False
    hour = now.astimezone(_zone(settings)).hour
    return start <= hour < end if start < end else hour >= start or hour < end


def is_paused(settings: HeartbeatSettings, now: datetime) -> bool:
    return bool(settings.paused_until and now < settings.paused_until)


def _norm(text: str) -> str:
    return re.sub(r"\W+", " ", text.casefold()).strip()


def _half_hour_up(moment: datetime) -> datetime:
    moment = moment.replace(second=0, microsecond=0)
    return moment + timedelta(minutes=30 - moment.minute % 30) if moment.minute % 30 else moment


def presets(user: UserDoc, now: datetime | None = None) -> list[ReminderPreset]:
    """The reschedule buttons: in an hour, this evening, tomorrow morning. Worked out in the person's
    own zone, rounded to the heartbeat's half hour (it is the finest it can deliver), and never offering a
    time that has passed or is the same as another."""
    now = now or utcnow()
    zone = _zone(user.heartbeat)
    local = now.astimezone(zone)
    wanted = [("hour", "In an hour", _half_hour_up(local + timedelta(hours=1)))]
    evening = local.replace(hour=18, minute=0, second=0, microsecond=0)
    if evening - local >= timedelta(hours=2):
        wanted.append(("evening", "This evening", evening))
    morning = (local + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    wanted.append(("tomorrow", "Tomorrow morning", morning))
    return [ReminderPreset(key=k, label=label, due_at=to_utc_seconds(when)) for k, label, when in wanted]


def out(user: UserDoc, doc: ReminderDoc, duplicate: bool = False, now: datetime | None = None) -> ReminderOut:
    show = doc.state in OPEN_STATES and not doc.is_note
    return ReminderOut.of(doc, presets(user, now) if show else None, duplicate)


async def create(user: UserDoc, body: ReminderCreate, now: datetime | None = None) -> tuple[ReminderDoc, bool]:
    """Make a reminder, or return the one it duplicates. Returns (reminder, duplicate)."""
    now = now or utcnow()
    due = body.due_at
    if due is not None:
        if due < now - PAST_GRACE:
            raise InvalidRequest(
                f"due_at {due.isoformat()} is more than an hour in the past (it is now {now.replace(microsecond=0).isoformat()}); "
                "check the date and the time zone"
            )
        if due > now + MAX_AHEAD:
            raise InvalidRequest("due_at is more than two years away; check the date")
    pending = await reminders_db.list_for_user(user.id, ("pending",))
    key = _norm(body.text)
    for existing in pending:
        same_time = (existing.due_at is None and due is None) or (
            existing.due_at is not None and due is not None and abs(existing.due_at - due) <= DUPLICATE_WINDOW
        )
        if same_time and _norm(existing.text) == key:
            return existing, True
    if len(pending) >= MAX_PENDING:
        raise InvalidRequest(f"there are already {MAX_PENDING} pending reminders; cancel some first")
    reminder = ReminderDoc(
        user_id=user.id,
        text=body.text,
        due_at=due,
        explicit_time=bool(body.explicit_time) if due else False,
        source=ReminderSource(**body.source.model_dump()),
        expires_at=None if due else (body.expires_at or to_utc_seconds(now + NOTE_LIFETIME)),
    )
    await reminders_db.insert(reminder)
    return reminder, False


async def get(user_id: str, reminder_id: str) -> ReminderDoc:
    """Someone else's reminder is a NotFound, never a 403 (which would confirm it exists)."""
    reminder = await reminders_db.find(reminder_id)
    if reminder is None or reminder.user_id != user_id:
        raise NotFound("reminder", reminder_id)
    return reminder


async def list_for(user: UserDoc, state: str | None = None, now: datetime | None = None) -> list[ReminderDoc]:
    """`pending` (the default view), another state, or `open` for pending and fired together."""
    await expire_notes(user.id, now)
    if state == "open":
        states: tuple[str, ...] | None = OPEN_STATES
    elif state:
        states = (state,)
    else:
        states = ("pending",)
    return await reminders_db.list_for_user(user.id, states)


async def update(user: UserDoc, reminder_id: str, body: ReminderUpdate, now: datetime | None = None) -> ReminderDoc:
    now = now or utcnow()
    reminder = await get(user.id, reminder_id)
    wanted = body.state or ("pending" if body.due_at else reminder.state)
    if reminder.state not in OPEN_STATES:
        raise InvalidTransition("reminder", reminder_id, reminder.state, wanted)
    if body.state == "cancelled" and reminder.state != "pending":
        raise InvalidTransition("reminder", reminder_id, reminder.state, "cancelled")
    changes: dict = {"updated_at": now}
    if body.text:
        changes["text"] = body.text
    if body.state:
        changes["state"] = body.state
    elif body.due_at is not None:
        if body.due_at < now - PAST_GRACE:
            raise InvalidRequest("due_at is more than an hour in the past; check the date and the time zone")
        if body.due_at > now + MAX_AHEAD:
            raise InvalidRequest("due_at is more than two years away; check the date")
        # Going again: it is pending, not yet fired, and has no expiry (it has a time now).
        changes.update(
            state="pending", due_at=body.due_at, explicit_time=bool(body.explicit_time),
            fired_at=None, late=False, expires_at=None,
        )
        if reminder.alert_id:
            await _settle_alert(reminder.alert_id)
    updated = reminder.model_copy(update=changes)
    await reminders_db.replace(updated)
    return updated


async def _settle_alert(alert_id: str) -> None:
    """A reminder that was put off has been dealt with by GACA: close its alert (when still open)
    so the inbox does not keep a nudge about something now scheduled for later."""
    alert = await alerts_db.find(alert_id)
    if alert is not None and alert_service.is_open(alert):
        await alerts_db.replace(alert.model_copy(update={"status": "done_by_gaca", "status_changed_at": utcnow()}))


async def expire_notes(user_id: str, now: datetime | None = None) -> int:
    now = now or utcnow()
    count = 0
    for note in await reminders_db.list_for_user(user_id, ("pending",)):
        if note.is_note and note.expires_at and note.expires_at <= now:
            await reminders_db.replace(note.model_copy(update={"state": "expired", "updated_at": now}))
            count += 1
    return count


def _local_label(settings: HeartbeatSettings, moment: datetime, now: datetime) -> str:
    local = moment.astimezone(_zone(settings))
    clock = local.strftime("%-I:%M %p")
    return clock if local.date() == now.astimezone(_zone(settings)).date() else f"{local:%a} {clock}"


async def fire_due(user: UserDoc, now: datetime | None = None) -> FireDueOut:
    """Turn every reminder that is due into an alert and a push. Safe to call as often and as
    concurrently as you like: each reminder is claimed with one conditional update, so it fires once.

    A reminder whose time the person named fires on time whatever the hour. A vague one ("tomorrow")
    waits for quiet hours and any pause to end, and fires on a later call.
    """
    now = now or utcnow()
    settings = user.heartbeat
    result = FireDueOut(fired=[], expired=await expire_notes(user.id, now))
    holding = in_quiet_hours(settings, now) or is_paused(settings, now)
    for due in await reminders_db.list_due(user.id, now):
        if holding and not due.explicit_time:
            result.held += 1
            continue
        late = now - due.due_at > LATE_AFTER
        fired = due.model_copy(update={"state": "fired", "fired_at": now, "late": late, "updated_at": now})
        if not await reminders_db.claim(due, fired):
            continue  # another call got it first
        try:
            alert, _ = await alert_service.create(user.id, _alert_for(settings, fired, now))
        except Exception:
            await reminders_db.release(fired, due)  # nothing was sent: try again next time
            raise
        fired = fired.model_copy(update={"alert_id": alert.id})
        await reminders_db.replace(fired)
        result.fired.append(out(user, fired, now=now))
    return result


def _alert_for(settings: HeartbeatSettings, reminder: ReminderDoc, now: datetime) -> AlertCreate:
    body = f"Was due {_local_label(settings, reminder.due_at, now)}" if reminder.late else None
    return AlertCreate(
        kind="reminder",
        title=reminder.text,
        body=body,
        # What they asked for, at the time they asked: worth a normal notification, not an urgent one.
        severity="warn" if reminder.explicit_time else "info",
        data={"reminder_id": reminder.id, "due_at": reminder.due_at.isoformat(), "late": reminder.late},
        # A reminder put off and fired again folds onto its alert and reopens it, rather than adding a second.
        dedupe_key=f"reminder:{reminder.id}",
        source="reminders",
        crew=reminder.source.crew,
    )
