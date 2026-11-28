"""Database model for a photo a page shows — the stored metadata. The bytes live in their own columns
(db/artifact_photos.py)."""
from __future__ import annotations

import secrets
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from .base import utcnow


def new_photo_id() -> str:
    """Short and recognisable in a page's data (`"photo": "ph_3k9x…"`), so the sweep can find references."""
    return "ph_" + secrets.token_hex(8)


class ArtifactPhotoDoc(BaseModel):
    id: str = Field(default_factory=new_photo_id)
    artifact_id: str
    content_type: str  # what we re-encoded it to, never what was uploaded
    width: int
    height: int
    size: int
    thumb_content_type: str
    thumb_width: int
    thumb_height: int
    thumb_size: int
    added_by: Literal["app", "agent", "web"]  # web: added on the cloud website, then brought home
    created_at: datetime = Field(default_factory=utcnow)
