"""Request/response models for artifacts — HTML pages an agent writes, plus their data.

An artifact is a page (`html`) and the data it keeps. The data comes in two shapes, use
whichever fits: one `state` document (simple pages), and keyed `collections` of small
documents (lists of things, where two writers adding at once shouldn't overwrite each other).
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_HTML_BYTES = 1_000_000
MAX_STATE_BYTES = 1_000_000
MAX_DOC_BYTES = 200_000
MAX_DOCS_PER_COLLECTION = 5000
MAX_BATCH_OPS = 200
MAX_VERSIONS_KEPT = 20

NAME_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"
DOC_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"
_NAME = re.compile(NAME_PATTERN)
_DOC_ID = re.compile(DOC_ID_PATTERN)


def check_name(value: str) -> str:
    if not _NAME.match(value):
        raise ValueError("names are lowercase letters, digits, '-' and '_' (max 64)")
    return value


def check_doc_id(value: str) -> str:
    if not _DOC_ID.match(value):
        raise ValueError("document ids are letters, digits, '-', '_' and '.' (max 64)")
    return value


class ArtifactCreate(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    html: str = Field(min_length=1)
    description: str | None = Field(default=None, max_length=500)
    # What the stored data looks like — read by whoever rewrites the page next.
    data_notes: str | None = Field(default=None, max_length=2000)
    crew: str | None = Field(default=None, max_length=100)
    # The chat / alert this page belongs to (shown with them).
    session_id: str | None = Field(default=None, max_length=64)
    alert_id: str | None = Field(default=None, max_length=64)
    # Seed data so the page opens with something in it.
    state: dict[str, Any] = Field(default_factory=dict)
    collections: dict[str, dict[str, dict[str, Any]]] = Field(
        default_factory=dict,
        description="collection name -> {document id -> document}. Seeds keyed documents.",
    )

    @field_validator("title")
    @classmethod
    def _tidy_title(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v:
            raise ValueError("title can't be blank")
        return v

    @field_validator("html")
    @classmethod
    def _html_size(cls, v: str) -> str:
        if len(v.encode()) > MAX_HTML_BYTES:
            raise ValueError(f"html is over {MAX_HTML_BYTES // 1000} KB")
        return v

    @model_validator(mode="after")
    def _check_seed(self) -> "ArtifactCreate":
        for name, docs in self.collections.items():
            check_name(name)
            for doc_id in docs:
                check_doc_id(doc_id)
        return self


class ArtifactPatch(BaseModel):
    """Only the fields you send change. Sending new `html` makes a new version (the old one
    is kept — see /versions); the data is never touched by a page rewrite."""
    title: str | None = Field(default=None, min_length=1, max_length=120)
    html: str | None = Field(default=None, min_length=1)
    description: str | None = Field(default=None, max_length=500)
    data_notes: str | None = Field(default=None, max_length=2000)
    note: str | None = Field(default=None, max_length=200, description="What changed, for the version list")
    archived: bool | None = None

    @field_validator("html")
    @classmethod
    def _html_size(cls, v: str | None) -> str | None:
        if v is not None and len(v.encode()) > MAX_HTML_BYTES:
            raise ValueError(f"html is over {MAX_HTML_BYTES // 1000} KB")
        return v


class ArtifactOrder(BaseModel):
    """Put these artifacts first, in this order. Any you leave out keep their current order
    after them, so a partial list (e.g. just the visible ones) is fine."""
    ids: list[str] = Field(max_length=500)


class ArtifactOut(BaseModel):
    id: str
    title: str
    description: str | None = None
    data_notes: str | None = None
    crew: str | None = None
    session_id: str | None = None
    alert_id: str | None = None
    archived: bool = False
    # Where the owner dragged it in the Artifacts tab (0 = first); null until they place it.
    position: int | None = None
    version: int
    # Bumped by any data write; compare with the `rev` you last saw to learn if there's news.
    rev: int
    state_version: int
    created_at: datetime
    updated_at: datetime
    last_opened_at: datetime | None = None
    # The in-app address: put this in a chat message and the app shows it as a tappable chip.
    link: str = ""

    @model_validator(mode="after")
    def _link(self) -> "ArtifactOut":
        self.link = f"gaca://artifact/{self.id}"
        return self


class ArtifactList(BaseModel):
    artifacts: list[ArtifactOut] = []


class VersionOut(BaseModel):
    version: int
    created_at: datetime
    note: str | None = None
    size: int


class VersionList(BaseModel):
    current: int
    versions: list[VersionOut] = []


class Restore(BaseModel):
    version: int = Field(ge=1)


# ── data ─────────────────────────────────────────────────────────────────────
class StatePut(BaseModel):
    state: dict[str, Any]
    # Optimistic concurrency: send the `version` you read; a stale one is a 409.
    expected_version: int | None = Field(default=None, ge=0)


class StatePatch(BaseModel):
    """A JSON merge patch (RFC 7386): objects merge key by key, `null` deletes a key,
    anything else (including arrays) replaces."""
    patch: dict[str, Any]
    expected_version: int | None = Field(default=None, ge=0)


class StateOut(BaseModel):
    state: dict[str, Any] = {}
    version: int
    rev: int


class DocPut(BaseModel):
    data: dict[str, Any]
    expected_version: int | None = Field(default=None, ge=1)


class DocPatch(BaseModel):
    patch: dict[str, Any]
    expected_version: int | None = Field(default=None, ge=1)


class DocCreate(BaseModel):
    """Add a document with a generated id (or your own `id`, which must be unused)."""
    data: dict[str, Any]
    id: str | None = Field(default=None, pattern=DOC_ID_PATTERN)


class DocOut(BaseModel):
    id: str
    version: int
    created_at: datetime
    updated_at: datetime
    data: dict[str, Any]


class CollectionOut(BaseModel):
    collection: str
    rev: int
    docs: list[DocOut] = []


class DataSnapshot(BaseModel):
    """Everything the page stores. With `since_rev` equal to the current `rev`,
    `changed` is false and the rest is left out — cheap to poll."""
    rev: int
    changed: bool = True
    state: dict[str, Any] | None = None
    state_version: int | None = None
    collections: dict[str, list[DocOut]] | None = None


class BatchOp(BaseModel):
    op: Literal["set", "patch", "delete", "clear"]
    collection: str
    id: str | None = Field(default=None, description="Document id (not needed for `clear`)")
    data: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _shape(self) -> "BatchOp":
        check_name(self.collection)
        if self.op != "clear":
            if not self.id:
                raise ValueError(f"`{self.op}` needs an id")
            check_doc_id(self.id)
        if self.op in ("set", "patch") and self.data is None:
            raise ValueError(f"`{self.op}` needs data")
        return self


class Batch(BaseModel):
    ops: list[BatchOp] = Field(min_length=1, max_length=MAX_BATCH_OPS)


class BatchResult(BaseModel):
    applied: int
    rev: int


# ── events ───────────────────────────────────────────────────────────────────
class EventIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    seq: int
    name: str
    created_at: datetime
    handled: bool
    payload: dict[str, Any] = {}


class EventList(BaseModel):
    events: list[EventOut] = []


class EventsHandled(BaseModel):
    upto: int = Field(ge=0, description="Mark every event up to and including this seq as handled")


class HandledResult(BaseModel):
    handled: int


# ── page calls ───────────────────────────────────────────────────────────────
class ArtifactCall(BaseModel):
    """One `window.app.*` call from an artifact page, forwarded verbatim by the client's
    native bridge: the SDK's method name and its args object."""
    method: str = Field(max_length=64)
    args: dict[str, Any] = Field(default_factory=dict)
