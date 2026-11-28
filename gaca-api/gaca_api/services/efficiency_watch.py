"""The weekly efficiency review: one alert a week saying where the model tokens went and what
would cut them, with a button per suggestion (docs/efficiency.md).

A background task of the running app, like automation_watch. Once an hour it asks "is the review
due?": the owner's chosen weekday at or after the chosen hour (their time zone), or later that week
if the server was down then. The analysis is plain rules over the numbers (services/usage.py), so
it costs no tokens; only a tapped button wakes a crew member, which then makes the change with the
gaca-efficiency skill (backup first, undo on request).

It also closes the loop: when a change suggested the week before was made, this week's alert says
what it did to that automation's tokens, and offers to undo it if it didn't help.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from ..config import settings
from ..db import alerts as alerts_db
from ..db import meta as meta_db
from ..db import users as users_db
from ..models.alert import AlertDoc, SuggestedAction
from ..models.user import UserDoc
from ..schemas.alert import AlertCreate
from ..schemas.usage import UsageFinding, UsageLine, UsageReport
from . import alerts as alerts_service
from . import usage as usage_service

logger = logging.getLogger(__name__)

KIND = "efficiency.weekly"
_CHECK_SECONDS = 3600.0
# A change that cut its automation's tokens by less than this is "didn't help" (offered an undo).
_HELPED = 0.2


def week_key(local: datetime) -> str:
    year, week, _ = local.isocalendar()
    return f"{year}-W{week:02d}"


def dedupe_key(week: str) -> str:
    return f"efficiency:{week}"


def _zone(user: UserDoc):
    name = getattr(user.heartbeat, "timezone", None)
    if name:
        with suppress(Exception):
            return ZoneInfo(name)
    return datetime.now().astimezone().tzinfo


def is_due(local: datetime) -> bool:
    day, hour = settings.efficiency_weekday, settings.efficiency_hour
    return local.weekday() > day or (local.weekday() == day and local.hour >= hour)


def _short(tokens: int | None) -> str:
    return usage_service._short(int(tokens or 0))


def _crew_for_alert(user: UserDoc) -> str | None:
    names = [m.name for m in user.crew]
    if "default" in names:
        return "default"
    return user.default_crew or (names[0] if names else None)


def _profile_words(finding_crew: str) -> str:
    return f'Hermes profile "{finding_crew}"'


def _action_for(finding: UsageFinding, line: UsageLine | None) -> SuggestedAction | None:
    where = f'automation "{finding.name}" (job {finding.automation_id}) on {_profile_words(finding.crew)}'
    facts = ""
    if line is not None:
        facts = (f" In the last {7} days it ran {line.runs} times, {line.silent_runs} of those runs ended silent, it made "
                 f"{line.calls} model calls and used {_short(line.total_tokens)} tokens "
                 f"({round(line.share * 100)}% of everything the crew used).")
    params = {"crew": finding.crew, "automation_id": finding.automation_id, "name": finding.name,
              "rule": finding.rule, "baseline_tokens": line.total_tokens if line else None,
              "detail": f"~{_short(finding.estimated_weekly_savings)} tokens a week"}
    if finding.rule == "prerun":
        return SuggestedAction(
            label=f"Add a pre-run check to {finding.name}",
            kind="efficiency.prerun",
            params=params,
            prompt=(
                f"Use the gaca-efficiency skill. Make the {where} cheaper by giving it a pre-run check.{facts} "
                "Read the job's prompt, its skills and any script to see exactly what it checks. Write a small script "
                "that looks at the same thing cheaply and prints {\"wakeAgent\": false} as its last line when there is "
                "nothing for the model to do, and anything else when there is. Test it, then attach it with the skill's "
                "helper (it backs the job up first). Do not change what the job does, its prompt, its schedule or "
                "where it delivers. Reply in a few lines: what the script checks, when it still wakes the model, and "
                "how to undo it."
            ),
        )
    if finding.rule == "review":
        return SuggestedAction(
            label=f"Suggest savings for {finding.name}",
            kind="efficiency.review",
            params=params,
            closes=False,
            prompt=(
                f"Use the gaca-efficiency skill. Look at the {where} and work out how to cut its tokens without "
                f"changing what it does for me.{facts} Consider a pre-run check, loading fewer skills, a smaller "
                "model pinned to just this job, and its schedule. Do not change anything yet: reply with the two or "
                "three best changes, the rough saving of each, and what (if anything) I would notice."
            ),
        )
    return None


def _results(previous: AlertDoc | None, report: UsageReport) -> tuple[list[str], list[SuggestedAction]]:
    """Lines saying what last week's changes did, and an undo for any that didn't help."""
    if previous is None:
        return [], []
    lines: list[str] = []
    undo: list[SuggestedAction] = []
    by_id = {(line.crew, line.automation_id): line for line in report.lines if line.automation_id}
    for run in previous.runs:
        if run.state != "done" or run.action_index is None or run.action_index >= len(previous.suggested_actions):
            continue
        action = previous.suggested_actions[run.action_index]
        if action.kind != "efficiency.prerun":
            continue
        params = action.params or {}
        crew, job, name = params.get("crew"), params.get("automation_id"), params.get("name") or "it"
        before = params.get("baseline_tokens")
        line = by_id.get((crew, job))
        after = line.total_tokens if line else 0
        if not isinstance(before, int) or before <= 0:
            continue
        change = (before - after) / before
        lines.append(f"Since last week's pre-run check on {name}: {_short(before)} → {_short(after)} tokens "
                     f"({'-' if change >= 0 else '+'}{abs(round(change * 100))}%).")
        if change < _HELPED:
            undo.append(SuggestedAction(
                label=f"Undo the change to {name}",
                kind="efficiency.undo",
                params={"crew": crew, "automation_id": job, "name": name},
                prompt=(f'Use the gaca-efficiency skill to undo last week\'s change to the automation "{name}" '
                        f'(job {job}) on Hermes profile "{crew}": restore it from the latest backup the skill made '
                        "for that job, then tell me what was restored."),
            ))
    return lines, undo


def build_alert(user: UserDoc, report: UsageReport, previous: AlertDoc | None, week: str) -> AlertCreate | None:
    """The week's alert, or None when there is nothing worth saying."""
    results, undo = _results(previous, report)
    by_id = {(line.crew, line.automation_id): line for line in report.lines if line.automation_id}
    actions: list[SuggestedAction] = []
    operator_notes: list[str] = []
    for finding in report.findings:
        if finding.rule == "heartbeat_prerun":
            operator_notes.append(f"{finding.title}: {finding.detail}")
            continue
        action = _action_for(finding, by_id.get((finding.crew, finding.automation_id)))
        if action is not None:
            actions.append(action)
    actions.extend(undo)
    if not actions and not results and not operator_notes:
        return None

    saving = sum(f.estimated_weekly_savings for f in report.findings)
    parts = []
    trend = ""
    if report.previous_total_tokens:
        change = (report.total_tokens - report.previous_total_tokens) / report.previous_total_tokens
        trend = f" ({'+' if change >= 0 else '-'}{abs(round(change * 100))}% on the week before)"
    parts.append(f"Your crew used {_short(report.total_tokens)} model tokens in the last {report.days} days{trend}.")
    top = [line for line in report.lines[:3] if line.total_tokens]
    if top:
        parts.append("Most went to: " + "; ".join(
            f"{line.name} ({line.crew}) {round(line.share * 100)}%" for line in top) + ".")
    if report.cost is not None:
        parts.append(f"On a pay-per-token plan that would be about ${report.cost.usd_per_month:.0f} a month "
                     f"({report.cost.label}).")
    parts.extend(results)
    parts.extend(operator_notes)
    if actions:
        parts.append("Each button makes one change, backed up first; nothing changes what your automations do. "
                     "Tap one to have it done.")
    if report.unavailable:
        parts.append(f"Couldn't read: {', '.join(report.unavailable)}.")
    title = (f"Improve your efficiency: save ~{_short(saving)} tokens a week" if saving
             else "Your weekly efficiency check")
    return AlertCreate(
        kind=KIND,
        severity="info",
        title=title,
        body="\n\n".join(parts),
        data={"week": week, "total_tokens": report.total_tokens,
              "previous_total_tokens": report.previous_total_tokens,
              "estimated_weekly_savings": saving,
              "usd_per_month_pay_per_token": report.cost.usd_per_month if report.cost else None},
        dedupe_key=dedupe_key(week),
        suggested_actions=actions,
        source="gaca-api",
        crew=_crew_for_alert(user),
    )


async def review(user: UserDoc, now: datetime | None = None, *, force: bool = False) -> AlertDoc | None:
    """Run this week's review if it is due and hasn't run; the alert it posted, if any."""
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(_zone(user))
    week = week_key(local)
    marker = f"efficiency:checked:{user.id}:{week}"
    if not force:
        if not is_due(local) or await meta_db.get(marker) is not None:
            return None
    report = await usage_service.report(user, days=7, until=now)
    if not report.lines and report.unavailable:
        return None  # couldn't read anything: try again next hour rather than mark the week done
    previous_week = week_key(local - timedelta(days=7))
    previous = await alerts_db.find_by_dedupe_key(user.id, dedupe_key(previous_week))
    payload = build_alert(user, report, previous, week)
    await meta_db.put(marker, {"at": now.isoformat(), "posted": payload is not None})
    if payload is None:
        return None
    alert, _ = await alerts_service.create(user.id, payload)
    return alert


async def _loop() -> None:
    await asyncio.sleep(120.0)
    while True:
        try:
            user = await users_db.first()
            if user is not None and user.crew:
                await review(user)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("efficiency review pass failed")
        await asyncio.sleep(_CHECK_SECONDS)


def start() -> asyncio.Task | None:
    if not settings.efficiency_review:
        return None
    return asyncio.create_task(_loop(), name="efficiency-review")


async def stop(task: asyncio.Task | None) -> None:
    if task is None:
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
