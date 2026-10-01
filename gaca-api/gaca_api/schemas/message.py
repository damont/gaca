"""Request/response models for messages — the client-facing shapes.

These never carry the `_id` alias; the client always sees `id`. They're built
from the DB models in the router (`from_attributes=True` lets us validate
straight off a MessageDoc).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_ATTACHMENTS_PER_MESSAGE = 4


@dataclass(frozen=True)
class ImageInput:
    """A photo handed to an agent engine with a user turn — neutral: raw bytes plus
    a sniffed content type. Each adapter encodes it however its engine wants."""
    content_type: str
    data: bytes


class MessageCreate(BaseModel):
    """Request body for POST /sessions/{id}/messages.

    A message needs text, photos, or both. Photos are uploaded first
    (POST /sessions/{id}/attachments) and referenced here by id.
    """
    content: str = ""
    role: str = "user"
    attachment_ids: list[str] = Field(default_factory=list, max_length=MAX_ATTACHMENTS_PER_MESSAGE)

    @model_validator(mode="after")
    def _needs_something(self) -> "MessageCreate":
        if not self.content.strip() and not self.attachment_ids:
            raise ValueError("a message needs text or at least one photo")
        return self


class AttachmentOut(BaseModel):
    """A photo on a message. `url` is relative to the API base (…/api/) and needs the
    same bearer token as every other route."""
    model_config = ConfigDict(from_attributes=True)

    id: str
    content_type: str
    size: int
    url: str = ""


class MessageOut(BaseModel):
    """A message as returned to the client."""
    model_config = ConfigDict(from_attributes=True)

    id: str
    session_id: str
    role: str
    content: str
    created_at: datetime
    attachments: list[AttachmentOut] = []

    @model_validator(mode="after")
    def _attachment_urls(self) -> "MessageOut":
        for a in self.attachments:
            a.url = f"sessions/{self.session_id}/attachments/{a.id}"
        return self


class MessageAccepted(BaseModel):
    """202 body: the message was stored; nothing consumes it yet."""
    turn_id: str
    status: str = "accepted"
