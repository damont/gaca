"""Neutral automation schema — GACA's own word for "something an agent does on a schedule".

Engines call these cron jobs, scheduled tasks, routines. To a person they are automations: a
name, when it runs, whether the last run worked, and a history of runs. Each engine's adapter
translates its own vocabulary into this (see agent/<engine>/translate_automations.py), so
clients never learn how any particular engine names things.

Read-mostly: a client can ask an automation to run now. Creating, editing, pausing and
resuming them is done by talking to the crew member in chat.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

RunStatus = Literal["ok", "failed", "running"]
# paused: switched off (by the crew member; a client can't do it) · running: a run is under way · failed: the last run failed ·
# done: a one-time automation that has finished · never_run: scheduled but hasn't fired yet · ok: the last run worked
AutomationStatus = Literal["ok", "failed", "running", "paused", "done", "never_run"]


class RunMark(BaseModel):
    """One past run, as small as a list row needs it (the row of coloured squares)."""
    id: str | None = None  # the same id the runs list uses
    status: RunStatus
    at: datetime | None = None


class Automation(BaseModel):
    id: str
    name: str
    crew: str = ""  # which crew member owns it (filled in by the service)
    schedule: str  # plain English: "Every day at 7:30 AM"
    cron_expr: str | None = None  # the raw expression, when it is one
    status: AutomationStatus
    last_status: RunStatus | None = None  # how the last run ended, even while paused
    last_run: datetime | None = None
    next_run: datetime | None = None
    kind: Literal["agent", "script"] = "agent"  # whether a model runs each time
    # The most recent runs (at most 10), oldest first, so the newest sits on the right.
    # Empty when the engine's history can't be read; the rest of the row still is true.
    recent: list[RunMark] = Field(default_factory=list)


class AutomationList(BaseModel):
    automations: list[Automation] = Field(default_factory=list)
    # Crew members whose automations couldn't be read, so a client can say so instead of
    # showing an empty list as if it were true.
    unavailable: list[str] = Field(default_factory=list)


class AutomationRun(BaseModel):
    id: str
    status: RunStatus
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_seconds: float | None = None
    # What the run said at the end, trimmed; None when it had nothing to say.
    summary: str | None = None
    # Why it failed, when it did.
    error: str | None = None
