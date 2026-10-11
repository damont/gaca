"""Request/response models for messages — the client-facing shapes.

These never carry the `_id` alias; the client always sees `id`. They're built
from the DB models in the router (`from_attributes=True` lets us validate
straight off a MessageDoc).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..utils import artifact_links
from .artifact import ArtifactRefOut

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


class ToolUseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    count: int
    errors: int = 0


class MessageActivity(BaseModel):
    """What the agent did to produce this reply. `label` is ready to display
    ("Ran terminal 3× · 41s"); `tools` is there for a client that wants more."""
    model_config = ConfigDict(from_attributes=True)

    tools: list[ToolUseOut]
    duration_seconds: float
    label: str


class MessageOut(BaseModel):
    """A message as returned to the client.

    `content` is the raw text, exactly as stored (and as sent back to the agent
    as history). Clients render `display_content` — the same text with artifact
    links taken out — and show `artifacts` as chips beneath it.
    """
    model_config = ConfigDict(from_attributes=True)

    id: str
    session_id: str
    role: str
    content: str
    display_content: str = ""
    created_at: datetime
    attachments: list[AttachmentOut] = []
    # Filled in by the service layer, which can look the artifacts up; built
    # straight from a MessageDoc it's empty.
    artifacts: list[ArtifactRefOut] = []
    activity: MessageActivity | None = None

    @model_validator(mode="after")
    def _derived(self) -> "MessageOut":
        for a in self.attachments:
            a.url = f"sessions/{self.session_id}/attachments/{a.id}"
        self.display_content = artifact_links.strip(self.content)
        return self


class MessageAccepted(BaseModel):
    """202 body: the message was stored; nothing consumes it yet."""
    turn_id: str
    status: str = "accepted"
