"""Request/response models for alerts — the client-facing shapes.

Note the asymmetry with models/alert.py: the DB model keeps `severity` and
`status` as plain strings, but the request schema pins them to Literals. Input
from an agent gets validated (a bogus severity is a 422, not a stored typo);
stored data stays permissive so an old row never fails to load.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

from ..models.alert import AlertLink, AlertRun, SuggestedAction
from .artifact import ArtifactRefOut


MAX_LINKS = 10


class AlertLinkIn(AlertLink):
    """A link as posted: web addresses only (the app opens them on tap), with a short label."""

    url: str = Field(..., min_length=1, max_length=1000)
    label: str | None = Field(default=None, max_length=80)

    @field_validator("url")
    @classmethod
    def _web_url(cls, value: str) -> str:
        value = value.strip()
        if not re.match(r"^https?://[^\s/]+", value, re.IGNORECASE):
            raise ValueError("url must be an http:// or https:// address")
        return value

    @field_validator("label")
    @classmethod
    def _tidy_label(cls, value: str | None) -> str | None:
        return (" ".join(value.split()) or None) if value else None


def _host_label(url: str) -> str:
    """What to call a link nobody labeled: its site, without "www."."""
    host = urlsplit(url).hostname or url
    return host[4:] if host.startswith("www.") else host


def render_value(value: Any) -> str:
    """One `data` value as display text. `data` is whatever the poster sent, so never
    assume its shape: strings as-is, other scalars as their JSON text, objects as
    "k: v, k2: v2" and arrays as "a, b" — recursively. (Ported from the Android client.)"""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return ", ".join(f"{k}: {render_value(v)}" for k, v in value.items())
    if isinstance(value, list):
        return ", ".join(render_value(v) for v in value)
    return json.dumps(value)


class DataRow(BaseModel):
    key: str
    value: str


class AlertCreate(BaseModel):
    """Request body for POST /api/alerts — what an agent posts."""

    kind: str = Field(..., min_length=1, max_length=100)
    title: str = Field(..., min_length=1, max_length=500)
    severity: Literal["info", "warn", "urgent"] = "info"
    body: str | None = None
    data: dict = Field(default_factory=dict)
    dedupe_key: str | None = Field(default=None, max_length=200)
    suggested_actions: list[SuggestedAction] = Field(default_factory=list)
    # Pages that go with this alert (the calendar week, the source email...). Up to 10;
    # an unlabeled one is shown under its site's name.
    links: list[AlertLinkIn] = Field(default_factory=list, max_length=MAX_LINKS)
    # An artifact you published (POST /api/artifacts) to show with this alert. `404` if it
    # isn't one of the caller's.
    artifact_id: str | None = Field(default=None, max_length=64)
    # Advisory label for who posted this. Not authenticated — ownership comes
    # from the token, this is only for telling posters apart in the UI.
    source: str = "unknown"
    # Which of the user's crew members posted this. Optional and separate
    # from `source` — a Hermes box can run multiple named crew members, and
    # this is what the client filters the inbox by, and what opens the right
    # crew member's chat when the alert is taken to one.
    #
    # `agent` is accepted as an alias: the Hermes alerts helper has always sent
    # the posting profile as `agent`, which this model silently ignored, so alerts
    # from skills carried no crew at all. If both are given, `crew` wins.
    crew: str | None = Field(
        default=None, max_length=100, validation_alias=AliasChoices("crew", "agent")
    )

    @field_validator("crew")
    @classmethod
    def _blank_crew_is_none(cls, value: str | None) -> str | None:
        return (value.strip() or None) if value else None


class AlertOut(BaseModel):
    """An alert as returned to the client."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    user_id: str
    created_at: datetime
    source: str
    crew: str | None = None
    # True when `crew` names a crew member the user has since removed; the alert stays as history.
    crew_removed: bool = False
    kind: str
    severity: str
    title: str
    body: str | None = None
    data: dict = {}
    dedupe_key: str | None = None
    suggested_actions: list[SuggestedAction] = []
    # Every link has a label here, even if the poster didn't give one.
    links: list[AlertLink] = []
    artifact_id: str | None = None
    # `data`'s top-level entries, in order, ready to show as label/value rows.
    data_rows: list[DataRow] = []
    # `artifact_id` resolved for its chip (see ArtifactRefOut) — null when there's none.
    artifact: ArtifactRefOut | None = None

    # `new` while open; otherwise how it ended — done_by_gaca / done_by_me / noted / not_useful.
    status: str
    # Same as `status`. Kept so apps built before alert actions keep reading it; new code uses `status`.
    effective_status: str
    # True until the alert is closed. Saves a client from knowing the list of closed reasons.
    open: bool = True
    status_changed_at: datetime
    # The chat tied to this alert, if any. It always gets the alert as context.
    session_id: str | None = None
    # What was tried on it, oldest first; each run carries its short result.
    runs: list[AlertRun] = []
    resolution_note: str | None = None
    parent_alert_id: str | None = None

    @model_validator(mode="after")
    def _derived(self) -> "AlertOut":
        self.open = self.status == "new"
        self.links = [
            AlertLink(url=link.url, label=link.label or _host_label(link.url)) for link in self.links
        ]
        self.data_rows = [DataRow(key=k, value=render_value(v)) for k, v in self.data.items()]
        return self


class AlertList(BaseModel):
    alerts: list[AlertOut] = []


class AlertResolve(BaseModel):
    """Body for act / dismiss — an optional note about how it ended."""

    note: str | None = Field(default=None, max_length=2000)


class AlertClose(BaseModel):
    """Body for POST /{id}/close — how the person is closing the alert."""

    reason: Literal["done_by_me", "noted", "not_useful"]
    note: str | None = Field(default=None, max_length=2000)


class AlertActionRun(BaseModel):
    """Body for POST /{id}/actions/{index}/run — optional extra instruction layered on the
    action's own prompt, and an explicit crew choice (same rules as delegate)."""

    instruction: str | None = Field(default=None, max_length=2000)
    crew: str | None = Field(default=None, max_length=100)


class AlertDelegate(BaseModel):
    """Body for POST /{id}/delegate (deprecated: use /actions/{index}/run, or /run for
    free text) — all fields optional.

    `suggested_action_index` is a convenience: it points at one of the alert's
    own `suggested_actions` rather than making the caller retype its prompt.
    `instruction` layers free text on top (or stands alone with no index).

    `crew` names which of the user's configured crew members should handle
    this — an explicit choice, so an unknown name is a real error (422),
    unlike the alert's own `crew` field which is soft-matched. Omit it to
    fall back to the alert's own `crew`, then the account's default.
    """

    instruction: str | None = Field(default=None, max_length=2000)
    suggested_action_index: int | None = Field(default=None, ge=0)
    crew: str | None = Field(default=None, max_length=100)


class DelegationCreate(BaseModel):
    """Body for POST /alerts/delegations — "hand this to a crew member" with nothing
    noticed first: the API raises the alert and delegates it in one step."""

    instruction: str = Field(..., max_length=2000)
    # Who handles it (and who the alert is attributed to); omit for the account default.
    # Same rules as delegate: naming one that isn't configured is a 422.
    crew: str | None = Field(default=None, max_length=100)
    source: str = Field(default="app", max_length=100)

    @field_validator("instruction")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("instruction can't be blank")
        return value


class AlertTakeToChat(BaseModel):
    """Body for POST /{id}/take-to-chat — all fields optional.

    `suggested_action_index` seeds the draft with one of the alert's own suggested
    actions; `crew` is an explicit crew-member choice (same rules as delegate).
    """

    suggested_action_index: int | None = Field(default=None, ge=0)
    crew: str | None = Field(default=None, max_length=100)


class TakeToChatOut(BaseModel):
    """Result of taking an alert to a chat: the (now handled) alert and the chat to open. The
    chat always carries the alert as context, so `draft` is only ever a short action line."""

    alert: AlertOut
    session_id: str
    # Only the one picked action's prompt; None otherwise (the alert itself needs no retyping).
    draft: str | None = None
