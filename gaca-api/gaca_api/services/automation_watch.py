"""Raise an alert when a crew member's scheduled automation fails.

A background task of the running app. Every minute it asks each crew member's engine for its
automations (the same read the Automations tab does), and for a run that FAILED recently it
posts one alert, which the app shows like any other: with a push notification, and with "take
to chat" to ask the crew member what went wrong.

No state of its own. The alert's `dedupe_key` names the run (`automation:<crew>:<job>:<run>`), so
a failure is announced once however many times it is seen, and one you dismissed stays dismissed
(posting the same key again would put it back in the inbox, so a run that already has an alert is
left alone, not re-posted). A failure older than the window is not announced: it is from before
this was switched on, or while the server was down, and a notification now would be stale news.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import datetime, timezone

from ..config import settings
from ..db import alerts as alerts_db
from ..db import users as users_db
from ..models.alert import SuggestedAction
from ..models.user import UserDoc
from ..schemas.alert import AlertCreate
from ..schemas.automation import Automation, AutomationRun, RunMark
from . import alerts as alerts_service
from . import automations as automations_service

logger = logging.getLogger(__name__)

_BODY_ERROR_CHARS = 300


def dedupe_key(automation: Automation, run_id: str) -> str:
    return f"automation:{automation.crew}:{automation.id}:{run_id}"


def _when(at: datetime) -> str:
    return at.strftime("%a %b %-d, %-I:%M %p")


def _fresh_failure(automation: Automation, now: datetime) -> RunMark | None:
    """The automation's latest run, if it failed within the window and we can name it."""
    if not automation.recent:
        return None
    last = automation.recent[-1]
    if last.status != "failed" or not last.id or last.at is None or last.at.tzinfo is None:
        return None
    if (now - last.at).total_seconds() > settings.automation_alert_window_hours * 3600:
        return None
    return last


def _alert_for(automation: Automation, mark: RunMark, run: AutomationRun | None) -> AlertCreate:
    when = _when(mark.at)
    error = (run.error if run and run.error else None)
    body = f"It was due {automation.schedule.lower()}. The run at {when} failed"
    body += f": {error[:_BODY_ERROR_CHARS]}" if error else " without saying why."
    detail = f" Error: {error}" if error else ""
    return AlertCreate(
        kind="automation_failed",
        severity="warn",
        title=f"{automation.name} failed",
        body=body,
        data={"automation": automation.name, "crew": automation.crew, "run": when},
        dedupe_key=dedupe_key(automation, mark.id),
        suggested_actions=[SuggestedAction(
            label="Find out why",
            prompt=(
                f'The scheduled automation "{automation.name}" (job {automation.id}) failed at {when}.{detail} '
                "Work out why it failed and tell me what's wrong. Fix it if you safely can, and say whether it "
                "is worth running again now."
            ),
        )],
        source="gaca-api",
        crew=automation.crew or None,
    )


async def _run_detail(user: UserDoc, automation: Automation, run_id: str) -> AutomationRun | None:
    """The failed run's own record (its error text). Best effort: an alert without the reason
    still says which automation failed and when."""
    try:
        runs = await automations_service.runs(user, automation.crew, automation.id, 10)
    except Exception as error:  # noqa: BLE001 — never let a missing detail cost the alert
        logger.info("no detail for %s run %s: %s", automation.id, run_id, error)
        return None
    return next((r for r in runs if r.id == run_id), None)


async def check(user: UserDoc, now: datetime | None = None) -> int:
    """Post an alert for each recent failed run that doesn't have one yet; how many were posted."""
    now = now or datetime.now(timezone.utc)
    posted = 0
    listed = await automations_service.list_all(user, include_done=True)
    for automation in listed.automations:
        mark = _fresh_failure(automation, now)
        if mark is None:
            continue
        if await alerts_db.find_by_dedupe_key(user.id, dedupe_key(automation, mark.id)) is not None:
            continue
        run = await _run_detail(user, automation, mark.id)
        await alerts_service.create(user.id, _alert_for(automation, mark, run))
        posted += 1
    return posted


async def _loop() -> None:
    # A first look soon after start, then steady; one bad pass is logged and the next still runs.
    await asyncio.sleep(min(30.0, settings.automation_watch_seconds))
    while True:
        try:
            user = await users_db.first()
            if user is not None and user.crew:
                await check(user)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("automation watch pass failed")
        await asyncio.sleep(settings.automation_watch_seconds)


def start() -> asyncio.Task | None:
    if not settings.automation_alerts:
        return None
    return asyncio.create_task(_loop(), name="automation-watch")


async def stop(task: asyncio.Task | None) -> None:
    if task is None:
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
