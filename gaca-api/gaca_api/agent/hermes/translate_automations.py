"""Hermes cron jobs and execution rows -> neutral automations (schemas/automation.py).

The only module that knows how Hermes names the facts about a scheduled job. The adapter
fetches (the gateway's /api/jobs, plus raw rows from the box's cron/executions.db); this maps.
Every function tolerates missing or odd fields, so a Hermes upgrade that adds or drops a key
degrades one detail, never the whole list.

Raw shapes, confirmed live against hermes-agent 0.21.5:

    GET /api/jobs?include_disabled=true -> {"jobs":[{"id","name","schedule":{"kind","expr","display"},
        "enabled","state":"scheduled|paused|completed|error|running","paused_at","next_run_at",
        "last_run_at","last_status":"ok|error"|null,"last_error","no_agent",
        "latest_execution":{...an execution row...}|null}]}
    execution row (cron/executions.db) -> {"id","job_id","status":"claimed|running|completed|failed|unknown",
        "claimed_at","started_at","finished_at","error","delivery_outcome","output": <tail of the run's
        cron/output/<job>/<time>.md, when asked for>}

A run's output file is "# Cron Job …", the metadata, "## Prompt" and the whole prompt (often many KB
of skill text), then "## Response" and what the agent said. Only the response is worth showing.
"""
from __future__ import annotations

import re
from datetime import datetime

from ...schemas.automation import Automation, AutomationRun, AutomationStatus, RunMark, RunStatus
from . import translate_profile as tp

_SUMMARY_CHARS = 600
_ERROR_CHARS = 500
_RESPONSE_HEADER = re.compile(r"^## Response[ \t]*\n", re.MULTILINE)
# Script jobs have no model response: the file is the header, "---", then the script's output.
_SCRIPT_BODY = re.compile(r"^---[ \t]*\n", re.MULTILINE)
# What an agent replies when it ran and had nothing to say. Not a summary.
_NOTHING = {"[SILENT]", "[silent]"}


def _dt(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def run_status(raw: object) -> RunStatus:
    """claimed/running -> running; completed -> ok; failed and unknown (interrupted) -> failed."""
    if raw in ("claimed", "running"):
        return "running"
    return "ok" if raw == "completed" else "failed"


def _job_result(raw: object) -> RunStatus | None:
    """A job's own `last_status` ("ok"/"error"/None)."""
    if raw is None:
        return None
    return "ok" if raw == "ok" else "failed"


def excerpt(output: str | None) -> str | None:
    """What the run said at the end, trimmed to a few lines; None if it had nothing to say."""
    if not output:
        return None
    match = None
    for match in _RESPONSE_HEADER.finditer(output):
        pass  # the last "## Response": a prompt may quote the word
    if match is not None:
        body = output[match.end():]
    elif (script := _SCRIPT_BODY.search(output)) is not None:
        body = output[script.end():]
    else:
        body = output  # only the tail of a long response was read: all of it is response
    body = body.strip()
    if not body or body in _NOTHING:
        return None
    return body if len(body) <= _SUMMARY_CHARS else body[: _SUMMARY_CHARS - 1].rstrip() + "…"


def _trim_error(error: object) -> str | None:
    if not isinstance(error, str) or not error.strip():
        return None
    text = error.strip()
    return text if len(text) <= _ERROR_CHARS else text[: _ERROR_CHARS - 1].rstrip() + "…"


def runs(rows: list[dict]) -> list[AutomationRun]:
    """Execution rows (newest first, as read) -> run history."""
    out: list[AutomationRun] = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        started = _dt(row.get("started_at")) or _dt(row.get("claimed_at"))
        finished = _dt(row.get("finished_at"))
        status = run_status(row.get("status"))
        duration = None
        if started and finished:
            try:
                duration = max(0.0, (finished - started).total_seconds())
            except TypeError:  # one side has no timezone
                duration = None
        out.append(AutomationRun(
            id=str(row["id"]),
            status=status,
            started_at=started,
            finished_at=finished,
            duration_seconds=duration,
            summary=excerpt(row.get("output")) if status == "ok" else None,
            error=_trim_error(row.get("error")) if status == "failed" else None,
        ))
    return out


def _status(job: dict, latest: RunStatus | None, has_run: bool) -> AutomationStatus:
    state = job.get("state")
    paused = (not job.get("enabled", True) and state != "completed" and state != "error") or state == "paused"
    if paused:
        return "paused"
    if latest == "running" or state == "running":
        return "running"
    if latest == "failed" or state == "error":
        return "failed"
    if state == "completed":
        return "done"
    if not has_run:
        return "never_run"
    return "ok"


def automations(
    jobs_response: dict, rows_by_job: dict[str, list[dict]] | None = None, *, include_done: bool = False
) -> list[Automation]:
    """The gateway's job list, plus (optionally) each job's recent execution rows newest first, as
    neutral automations: soonest next run first, jobs with nothing scheduled last."""
    out: list[Automation] = []
    for job in jobs_response.get("jobs") or []:
        if not isinstance(job, dict) or not job.get("id"):
            continue
        job_id = str(job["id"])
        rows = (rows_by_job or {}).get(job_id) or []
        latest_row = rows[0] if rows else (job.get("latest_execution") or None)
        latest = run_status(latest_row.get("status")) if isinstance(latest_row, dict) and latest_row.get("status") else None
        if latest is None:
            latest = _job_result(job.get("last_status"))
        last_run = _dt(job.get("last_run_at"))
        if isinstance(latest_row, dict):
            last_run = _dt(latest_row.get("started_at")) or _dt(latest_row.get("claimed_at")) or last_run
        schedule = job.get("schedule") or {}
        status = _status(job, latest, has_run=latest is not None or last_run is not None)
        if status == "done" and not include_done:
            continue
        out.append(Automation(
            id=job_id,
            name=job.get("name") or job_id,
            schedule=tp.schedule_text(job),
            cron_expr=schedule.get("expr") if schedule.get("kind", "cron") == "cron" and schedule.get("expr") else None,
            status=status,
            last_status=latest,
            last_run=last_run,
            next_run=_dt(job.get("next_run_at")) if status not in ("paused", "done") else None,
            kind="script" if job.get("no_agent") else "agent",
            recent=[
                RunMark(id=str(r["id"]) if r.get("id") else None, status=run_status(r.get("status")), at=_dt(r.get("started_at")) or _dt(r.get("claimed_at")))
                for r in reversed(rows[:10])  # rows are newest first; the strip reads oldest to newest
                if isinstance(r, dict)
            ],
        ))
    out.sort(key=lambda a: (a.next_run is None, a.next_run.timestamp() if a.next_run else 0.0, a.name))
    return out
