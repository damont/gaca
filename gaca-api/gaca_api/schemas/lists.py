"""Request/response models for lists — the client-facing shapes.

Lists are for anything you keep on a list: shopping (Costco, Home Depot), gifts (Christmas),
to-dos. The API owns the rules (matching "the Costco run" to a list called "Costco", not
adding milk twice) so every client — the app, an agent — behaves the same.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

ListKind = Literal["shopping", "gift", "todo", "general"]
MAX_ITEMS_PER_ADD = 100
MAX_ITEMS_PER_LIST = 500


def _http_only(v: str | None) -> str | None:
    if v is not None and not re.match(r"^https?://", v, re.IGNORECASE):
        raise ValueError("url must start with http:// or https://")
    return v


class ItemIn(BaseModel):
    text: str = Field(min_length=1, max_length=200)
    quantity: str | None = Field(default=None, max_length=60)
    note: str | None = Field(default=None, max_length=500)
    category: str | None = Field(default=None, max_length=60)
    for_person: str | None = Field(default=None, max_length=80)
    url: str | None = Field(default=None, max_length=1000)
    price: str | None = Field(default=None, max_length=40)

    _check_url = field_validator("url")(_http_only)

    @field_validator("text")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("text can't be blank")
        return v.strip()


class ItemPatch(BaseModel):
    """Only the fields you send change; send null to clear an optional one."""
    text: str | None = Field(default=None, min_length=1, max_length=200)
    done: bool | None = None
    quantity: str | None = Field(default=None, max_length=60)
    note: str | None = Field(default=None, max_length=500)
    category: str | None = Field(default=None, max_length=60)
    for_person: str | None = Field(default=None, max_length=80)
    url: str | None = Field(default=None, max_length=1000)
    price: str | None = Field(default=None, max_length=40)

    _check_url = field_validator("url")(_http_only)


class ItemOut(BaseModel):
    id: str
    text: str
    quantity: str | None = None
    note: str | None = None
    category: str | None = None
    for_person: str | None = None
    url: str | None = None
    price: str | None = None
    done: bool = False
    created_at: datetime
    # Set only on a just-added item: an existing, different item that looks related
    # ("2% milk" when you added "milk"), so the caller can mention it.
    similar_to: str | None = None


class ListCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    kind: ListKind = "general"
    notes: str | None = Field(default=None, max_length=1000)

    @field_validator("name")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name can't be blank")
        return v.strip()


class ListPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    kind: ListKind | None = None
    notes: str | None = Field(default=None, max_length=1000)
    archived: bool | None = None


class ListSummary(BaseModel):
    id: str
    name: str
    kind: str
    notes: str | None = None
    open_count: int = 0
    done_count: int = 0
    archived: bool = False
    updated_at: datetime


class ListSummaries(BaseModel):
    lists: list[ListSummary] = []


class ListMatch(BaseModel):
    """GET /lists/match — which of your lists a spoken/typed name means."""
    match: ListSummary | None = None
    # More than one plausible list and no clear winner: ask which one.
    candidates: list[ListSummary] = []


class ListDetail(ListSummary):
    """GET /lists/{id}: open items first (grouped by category), then checked-off ones."""
    items: list[ItemOut] = []


class AddItems(BaseModel):
    items: list[ItemIn] = Field(min_length=1, max_length=MAX_ITEMS_PER_ADD)


class Duplicate(BaseModel):
    text: str
    existing: ItemOut


class AddResult(BaseModel):
    list: ListSummary
    added: list[ItemOut] = []
    reopened: list[ItemOut] = []  # were checked off; adding them again un-checks them
    duplicates: list[Duplicate] = []  # already on the list (open); nothing changed


class ByText(BaseModel):
    """Act on items by what they're called, not by id."""
    texts: list[str] = Field(min_length=1, max_length=MAX_ITEMS_PER_ADD)
    done: bool = True  # used by /items/check


class Ambiguous(BaseModel):
    query: str
    candidates: list[ItemOut]


class ByTextResult(BaseModel):
    matched: list[ItemOut] = []
    ambiguous: list[Ambiguous] = []  # matched several items equally well: nothing was changed for these
    not_found: list[str] = []


class ClearResult(BaseModel):
    removed: int
