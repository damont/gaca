"""Database models for lists — the stored shape.

A list belongs to one user (`user_id`). Sharing a list with other people (a household's
Costco list, say) is deliberately not modeled yet; when it is, it will be a membership
alongside `user_id`, not a change to the items.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from .base import new_id, utcnow


class ListDoc(BaseModel):
    id: str = Field(default_factory=new_id)
    user_id: str
    name: str
    kind: str = "general"  # shopping | gift | todo | general (a display/behavior hint)
    notes: str | None = None
    archived: bool = False
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class ListItemDoc(BaseModel):
    id: str = Field(default_factory=new_id)
    list_id: str
    user_id: str
    text: str
    quantity: str | None = None  # free text: "2", "2 lb", "a dozen"
    note: str | None = None
    category: str | None = None  # store section / grouping: "produce", "dairy"...
    for_person: str | None = None  # gift lists: who it's for
    url: str | None = None
    price: str | None = None  # free text: "$24.99"
    done: bool = False
    done_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
