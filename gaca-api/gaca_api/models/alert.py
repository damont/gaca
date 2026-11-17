"""Database model for an alert — the stored shape (system of record).

An Alert is NOT a GacaEvent. A GacaEvent (schemas/event.py) is one frame of a
live agent run and lives for seconds; an Alert is a durable "you should know
about this" that lives until it's triaged. Different table, different lifetime,
different audience.
"""
from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field, field_validator, model_validator

from .base import new_id, utcnow

# An alert is open (`new`) or closed, and a closed alert says HOW it ended — that
# reason is what the heartbeat learns from:
#   done_by_gaca  an action ran and succeeded, or the alert was taken to chat
#   done_by_me    the person handled it themselves (the "an action could do this" gap)
#   noted         useful, nothing to do
#   not_useful    the person doesn't want this kind of thing
# Running an action is a RUN on the alert (AlertRun), not a status of it.
STATUSES = ("new", "done_by_gaca", "done_by_me", "noted", "not_useful")
CLOSED_STATUSES = ("done_by_gaca", "done_by_me", "noted", "not_useful")
# Reasons a PERSON can close an alert with. done_by_gaca is only ever set by the system.
PERSON_CLOSE_REASONS = ("done_by_me", "noted", "not_useful")

RUN_STATES = ("running", "done", "failed")

# Drives how loudly the client presents the alert. Unlike `kind`, this IS a
# closed set — clients switch on it.
SEVERITIES = ("info", "warn", "urgent")


class SuggestedAction(BaseModel):
    """One thing the poster proposes doing about the alert.

    `prompt` is the text handed back to the agent if the user picks this action,
    so it has to stand on its own — the agent won't see the button label.
    """

    label: str
    prompt: str
    # What sort of thing this does ("calendar.add", "email.draft", "reply"…). Open-ended like an
    # alert's `kind`: the client uses it for an icon and may ignore a kind it doesn't know.
    kind: str | None = None
    # Structured details (event title and time, a recipient…) a client can preview before the tap.
    # Never interpreted by the API; `prompt` is what the agent is actually given.
    params: dict = Field(default_factory=dict)


class AlertRun(BaseModel):
    """One time the person tapped an action (or typed a one-off instruction) and an agent
    went off to do it. The alert keeps these as its history of what was tried."""

    id: str = Field(default_factory=new_id)
    # Which of the alert's suggested actions this ran; None for a free-text instruction.
    action_index: int | None = None
    label: str
    state: str = "running"
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    # The agent's answer, short — what the app shows under the button.
    summary: str | None = None
    links: list["AlertLink"] = Field(default_factory=list)
    error: str | None = None
    # The chat that did the work. Opening it is how the person continues or inspects.
    session_id: str | None = None


class AlertLink(BaseModel):
    """A web page that goes with an alert — the calendar week an event is on, the email a
    suggestion came from, the dashboard that raised it. Stored permissively; requests are
    validated (schemas/alert.py:AlertLinkIn) and responses always carry a label."""

    url: str
    label: str | None = None


class AlertDoc(BaseModel):
    id: str = Field(default_factory=new_id)
    user_id: str
    created_at: datetime = Field(default_factory=utcnow)

    # Who posted it. Advisory metadata supplied by the caller, NOT authenticated
    # — our tokens carry only a subject and an expiry, so there's no key label
    # to read. `user_id` above is the real ownership boundary.
    source: str = "unknown"

    # Which of the user's crew members posted this, e.g. "toolshed" or
    # "trading" — distinct from `source` (which channel/mechanism posted it,
    # e.g. "hermes-skill" vs "manual"). Same trust level as `source`:
    # advisory, unauthenticated, just for telling crew members apart and
    # filtering the inbox by one of them.
    crew: str | None = None

    # Free-form dotted string: "disk.low", "email.flagged", "cal.conflict".
    # Deliberately not an enum — the agent must be able to invent a new kind
    # without an API release, so clients have to render unknown kinds sensibly.
    kind: str
    severity: str = "info"

    title: str
    body: str | None = None
    # Whatever the poster saw, verbatim. We never interpret it.
    data: dict = Field(default_factory=dict)

    # Optional idempotency handle. Re-posting with the same (user_id, dedupe_key)
    # updates this row instead of creating another — the difference between one
    # alert and one every time a five-minute cron fires.
    dedupe_key: str | None = None
    suggested_actions: list[SuggestedAction] = Field(default_factory=list)
    # Pages worth opening to act on this alert; the app shows them as tappable links.
    links: list[AlertLink] = Field(default_factory=list)
    # An artifact (HTML page, see models/artifact.py) that goes with this alert; the app
    # offers to open it.
    artifact_id: str | None = None

    status: str = "new"
    status_changed_at: datetime = Field(default_factory=utcnow)
    # The chat tied to this alert, once there is one (a run or take-to-chat opens it). The chat
    # always gets this alert as context.
    session_id: str | None = None
    # What was tried on this alert, oldest first.
    runs: list[AlertRun] = Field(default_factory=list)
    resolution_note: str | None = None
    # Set on a clarification raised against another alert.
    parent_alert_id: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_agent_field(cls, data):
        """Read rows written before the agent→crew rename (2026-09): the old
        `agent` key maps onto `crew` when `crew` isn't already present, so an
        already-posted alert's poster survives instead of reading back None.
        """
        if isinstance(data, dict) and "crew" not in data and "agent" in data:
            data["crew"] = data["agent"]
        return data

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_status(cls, data):
        """Read rows written before alert actions (2026-11): the old six states fold into
        the new five. Nothing is rewritten in storage; a row is normalised whenever it is read
        and saved in the new vocabulary the next time it changes.

        snoozed / delegated / completed were all "still open" -> new. `acted` meant the person
        or Hermes dealt with it: done_by_gaca when a chat or run was involved (including
        take-to-chat, the "Taken to chat" note), else done_by_me. dismissed -> not_useful.
        """
        if isinstance(data, dict):
            status = data.get("status")
            if status in ("snoozed", "delegated", "completed"):
                data = {**data, "status": "new"}
            elif status == "acted":
                involved = bool(data.get("session_id")) or data.get("resolution_note") == "Taken to chat"
                data = {**data, "status": "done_by_gaca" if involved else "done_by_me"}
            elif status == "dismissed":
                data = {**data, "status": "not_useful"}
        return data

    @field_validator("created_at", "status_changed_at")
    @classmethod
    def _ensure_aware(cls, value: datetime | None) -> datetime | None:
        """Force UTC on any naive datetime, so a naive/aware comparison can never raise."""
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
