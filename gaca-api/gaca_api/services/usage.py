"""How many model tokens the crew used, and what would make it cheaper. No FastAPI, no HTTP.

The numbers are read off the engine's box (provisioning/usage_read.py) for a window and the window
before it. The findings are plain rules, no model involved, so the review costs nothing to run and
says the same thing for the same numbers. See docs/efficiency.md for why each rule exists.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from ..config import settings
from ..models.user import UserDoc
from ..provisioning.base import ProvisioningError
from ..provisioning.select import provisioner_for
from ..schemas.usage import UsageCost, UsageFinding, UsageLine, UsageReport

logger = logging.getLogger(__name__)

HEARTBEAT = "gaca-heartbeat"
_SOURCES = {"api_server": "Chats from the app", "cli": "Command line", "oneshot": "One-off runs",
            "tui": "Terminal app", "telegram": "Telegram", "discord": "Discord"}

# The rules' thresholds, per week (scaled to the window).
PRERUN_MIN_RUNS = 14          # runs often enough that skipping some is worth a script
PRERUN_MIN_SILENT = 0.6       # most runs end with nothing to say
PRERUN_MIN_SHARE = 0.05       # and it is a noticeable part of the bill
REVIEW_MIN_SHARE = 0.25       # one automation is a quarter of everything
REVIEW_MAX_PER_CALL = 40_000  # or each model call sends a very large prompt
REVIEW_PER_CALL_MIN_SHARE = 0.05
MAX_FINDINGS = 4


def _line(crew: str, group: dict, jobs: dict[str, dict]) -> UsageLine:
    prompt = int(group.get("input_tokens", 0)) + int(group.get("cache_read_tokens", 0)) + int(group.get("cache_write_tokens", 0))
    output = int(group.get("output_tokens", 0))
    job = jobs.get(str(group.get("job_id"))) if group.get("kind") == "job" else None
    if group.get("kind") == "job":
        name = (job or {}).get("name") or group.get("name") or str(group.get("job_id"))
    else:
        name = _SOURCES.get(str(group.get("source")), str(group.get("source") or "Other"))
    return UsageLine(
        crew=crew,
        kind="automation" if group.get("kind") == "job" else "chat",
        automation_id=group.get("job_id"),
        name=name,
        runs=int(group.get("runs", 0)),
        silent_runs=int(group.get("silent_runs", 0)),
        calls=int(group.get("calls", 0)),
        prompt_tokens=prompt,
        cached_tokens=int(group.get("cache_read_tokens", 0)),
        output_tokens=output,
        total_tokens=prompt + output,
        model=group.get("model"),
        schedule=(job or {}).get("schedule_display"),
        has_prerun=bool(job and (job.get("script") or job.get("monitor_script") or job.get("monitor_url"))),
    )


async def _read(provisioner, crew: str, since: datetime, until: datetime) -> dict | None:
    return await asyncio.wait_for(
        provisioner.read_usage(crew, since=since.timestamp(), until=until.timestamp()), timeout=60.0
    )


def cost(lines: list[UsageLine], days: int) -> UsageCost:
    cached = sum(line.cached_tokens for line in lines)
    full = sum(line.prompt_tokens - line.cached_tokens for line in lines)
    output = sum(line.output_tokens for line in lines)
    usd = (full * settings.usage_compare_input + cached * settings.usage_compare_cached
           + output * settings.usage_compare_output) / 1_000_000
    return UsageCost(
        label=settings.usage_compare_label,
        input_per_million=settings.usage_compare_input,
        cached_per_million=settings.usage_compare_cached,
        output_per_million=settings.usage_compare_output,
        usd=round(usd, 2),
        usd_per_month=round(usd * 30 / max(days, 1), 2),
    )


def findings(lines: list[UsageLine], days: int) -> list[UsageFinding]:
    """The rules. Each automation gets at most one finding; biggest saving first."""
    scale = days / 7
    out: list[UsageFinding] = []
    for line in lines:
        if line.kind != "automation" or line.runs == 0:
            continue
        silent = line.silent_runs / line.runs
        per_call = (line.prompt_tokens / line.calls) if line.calls else 0
        if line.name == HEARTBEAT and not line.has_prerun and line.crew == "default":
            out.append(UsageFinding(
                rule="heartbeat_prerun", crew=line.crew, automation_id=line.automation_id, name=line.name,
                title="Give the heartbeat its pre-run check",
                detail=(f"The heartbeat woke the model {line.runs} times and {line.silent_runs} of those runs ended "
                        "silent. GACA ships a pre-run check that skips the model when there is nothing new; "
                        "it is added by running `gaca-api hermes connect` on the server."),
                estimated_weekly_savings=int(line.total_tokens * silent * 0.9 / scale),
            ))
        elif (not line.has_prerun and line.runs >= PRERUN_MIN_RUNS * scale and silent >= PRERUN_MIN_SILENT
              and line.share >= PRERUN_MIN_SHARE):
            out.append(UsageFinding(
                rule="prerun", crew=line.crew, automation_id=line.automation_id, name=line.name,
                title=f"Add a pre-run check to {line.name}",
                detail=(f"{line.silent_runs} of its {line.runs} runs ended with nothing to say, yet each one woke "
                        f"the model ({_short(line.total_tokens)} tokens, {round(line.share * 100)}% of everything). "
                        "A small script can look first and skip the model when nothing changed."),
                estimated_weekly_savings=int(line.total_tokens * silent * 0.8 / scale),
            ))
        elif line.share >= REVIEW_MIN_SHARE or (per_call >= REVIEW_MAX_PER_CALL and line.share >= REVIEW_PER_CALL_MIN_SHARE):
            why = (f"{round(line.share * 100)}% of all tokens" if line.share >= REVIEW_MIN_SHARE
                   else f"about {_short(int(per_call))} tokens sent per model call")
            out.append(UsageFinding(
                rule="review", crew=line.crew, automation_id=line.automation_id, name=line.name,
                title=f"Find savings in {line.name}",
                detail=(f"It used {_short(line.total_tokens)} tokens in {line.runs} runs ({why}). Worth a look at "
                        "its schedule, the skills it loads and the model it uses."),
                estimated_weekly_savings=int(line.total_tokens * 0.3 / scale),
            ))
    out.sort(key=lambda f: f.estimated_weekly_savings, reverse=True)
    return out[:MAX_FINDINGS]


def _short(tokens: int) -> str:
    if tokens >= 1_000_000:
        return f"{tokens / 1_000_000:.1f}M"
    if tokens >= 1_000:
        return f"{round(tokens / 1_000)}k"
    return str(tokens)


async def report(user: UserDoc, days: int = 7, until: datetime | None = None) -> UsageReport:
    """Usage for the last `days` days (ending `until`, default now), every crew member, with the
    window before for comparison, a pay-per-token estimate and the findings."""
    until = until or datetime.now(timezone.utc)
    since = until - timedelta(days=days)
    before = since - timedelta(days=days)
    out = UsageReport(since=since, until=until, days=days)
    provisioner = provisioner_for(user)
    if provisioner is None:
        out.unavailable = [m.name for m in user.crew]
        return out

    lines: list[UsageLine] = []
    previous: dict[tuple[str, str], int] = {}
    previous_total = 0
    for member in user.crew:
        try:
            now_data, before_data = await _read(provisioner, member.name, since, until), await _read(provisioner, member.name, before, since)
        except (ProvisioningError, ValueError, OSError, asyncio.TimeoutError) as error:
            logger.warning("couldn't read token use for %r: %s", member.name, error)
            out.unavailable.append(member.name)
            continue
        if now_data is None:
            out.unavailable.append(member.name)
            continue
        jobs = {str(j.get("id")): j for j in now_data.get("jobs", []) if isinstance(j, dict)}
        for group in (before_data or {}).get("groups", []):
            tokens = _line(member.name, group, jobs).total_tokens
            previous[(member.name, str(group.get("key")))] = tokens
            previous_total += tokens
        for group in now_data.get("groups", []):
            line = _line(member.name, group, jobs)
            line.previous_total_tokens = previous.get((member.name, str(group.get("key"))))
            lines.append(line)

    total = sum(line.total_tokens for line in lines)
    for line in lines:
        line.share = round(line.total_tokens / total, 4) if total else 0.0
    lines.sort(key=lambda line: line.total_tokens, reverse=True)
    out.lines = lines
    out.total_tokens = total
    out.previous_total_tokens = previous_total if previous_total else None
    out.cost = cost(lines, days)
    out.findings = findings(lines, days)
    return out
