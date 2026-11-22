"""HTTP layer for reminders. Thin: validate, call the service, shape the answer.

The agent token may use all of it (scope `reminders`): these are the owner's own, and the worst an
agent can do is make or cancel a nudge.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response

from ..models.user import UserDoc
from ..schemas.reminder import FireDueOut, ReminderCreate, ReminderList, ReminderOut, ReminderUpdate
from ..services import reminders as reminder_service
from ..utils.auth import get_current_user

router = APIRouter(prefix="/reminders", tags=["reminders"])


@router.post("", response_model=ReminderOut)
async def create_reminder(body: ReminderCreate, response: Response, user: UserDoc = Depends(get_current_user)):
    """Make a reminder (with `due_at`) or a "later" note (without). 201 when new; 200 with
    `duplicate: true` when the same one is already pending and nothing was made."""
    reminder, duplicate = await reminder_service.create(user, body)
    response.status_code = 200 if duplicate else 201
    return reminder_service.out(user, reminder, duplicate)


@router.get("", response_model=ReminderList)
async def list_reminders(
    user: UserDoc = Depends(get_current_user),
    # pending (default), fired, done, cancelled, expired, or `open` (pending and fired).
    state: str | None = Query(default=None, pattern="^(pending|fired|done|cancelled|expired|open)$"),
):
    reminders = await reminder_service.list_for(user, state)
    return ReminderList(reminders=[reminder_service.out(user, r) for r in reminders])


@router.post("/fire-due", response_model=FireDueOut)
async def fire_due(user: UserDoc = Depends(get_current_user)):
    """Fire everything that is due now. The heartbeat calls this every run; idempotent."""
    return await reminder_service.fire_due(user)


@router.get("/{reminder_id}", response_model=ReminderOut)
async def get_reminder(reminder_id: str, user: UserDoc = Depends(get_current_user)):
    return reminder_service.out(user, await reminder_service.get(user.id, reminder_id))


@router.patch("/{reminder_id}", response_model=ReminderOut)
async def update_reminder(reminder_id: str, body: ReminderUpdate, user: UserDoc = Depends(get_current_user)):
    """Reschedule (`due_at`, which also sets a fired one going again), edit `text`, or end it with
    `state`: `cancelled` (Undo) or `done`."""
    return reminder_service.out(user, await reminder_service.update(user, reminder_id, body))
