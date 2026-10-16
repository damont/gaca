"""Database models for artifacts — HTML pages an agent writes, plus the data they keep.

An artifact's HTML is a stateless view; everything the page remembers lives in the
API (see services/artifacts.py), so the page can be rewritten without losing data and
the agent can read and write the same data while the page is closed.
"""
from __future__ import annotations

from datetime import datetime

from typing import Any

from pydantic import BaseModel, Field

from .base import new_id, utcnow


class ArtifactDoc(BaseModel):
    """The descriptive part of an artifact (the HTML, state and counters are their own
    columns — see db/client.py)."""
    id: str = Field(default_factory=new_id)
    user_id: str
    title: str
    description: str | None = None
    # Which crew member made it, and the chat / alert it was made for (all advisory).
    crew: str | None = None
    session_id: str | None = None
    alert_id: str | None = None
    # A sentence or two on the shape of the stored data, for whoever rewrites the page
    # next ("state: {items:[{id,text,done}]}"), so a rewrite migrates data instead of
    # resetting it.
    data_notes: str | None = None
    # collection name -> its shape ({fields: {name: {type, required, enum, min, max, max_length}}, strict}); see
    # schemas/artifact.py CollectionSchema and services/artifact_schema.py. Stored as plain dicts in the doc JSON.
    schemas: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)
    last_opened_at: datetime | None = None
