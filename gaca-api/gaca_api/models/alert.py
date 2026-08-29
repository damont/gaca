"""Database model for an alert — the stored shape (system of record).

An Alert is NOT a GacaEvent. A GacaEvent (schemas/event.py) is one frame of a
live agent run and lives for seconds; an Alert is a durable "you should know
about this" that lives until it's triaged. Different table, different lifetime,
different audience.
"""
from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field, field_validator

from .base import new_id, utcnow

# The triage states. Four of them are the dispositions the user picks; `new` is
# the inbox. `acted` and `dismissed` are terminal.
STATUSES = ("new", "snoozed", "delegated", "acted", "dismissed")

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


class AlertDoc(BaseModel):
    id: str = Field(default_factory=new_id)
    user_id: str
    created_at: datetime = Field(default_factory=utcnow)

    # Who posted it. Advisory metadata supplied by the caller, NOT authenticated
    # — our tokens carry only a subject and an expiry, so there's no key label
    # to read. `user_id` above is the real ownership boundary.
    source: str = "unknown"

    # Which Hermes agent profile posted this, e.g. "david-alerts" or
    # "work-assistant" — distinct from `source` (which channel/mechanism
    # posted it, e.g. "hermes-skill" vs "manual"). Same trust level as
    # `source`: advisory, unauthenticated, just for telling agents apart and
    # filtering the inbox by one of them.
    agent: str | None = None

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

    status: str = "new"
    status_changed_at: datetime = Field(default_factory=utcnow)
    snooze_until: datetime | None = None
    # The chat session running a delegation, once there is one.
    session_id: str | None = None
    resolution_note: str | None = None
    # Set on a clarification raised against another alert.
    parent_alert_id: str | None = None

    @field_validator("created_at", "status_changed_at", "snooze_until")
    @classmethod
    def _ensure_aware(cls, value: datetime | None) -> datetime | None:
        """Force UTC on any naive datetime.

        Effective status compares snooze_until against now, and comparing a
        naive datetime with an aware one raises TypeError — which would be a 500
        on the most-called endpoint in the app. Normalizing on the way in means
        the comparison can never hit that.
        """
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
