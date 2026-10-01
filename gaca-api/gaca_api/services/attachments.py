"""Photo attachments for chat messages. No FastAPI, no HTTP.

Flow: the app uploads each photo (`store`), then sends a message that references
the returned ids; `claim` validates them and `runs.start_turn` attaches them.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from ..db import attachments as attachments_db
from ..models.attachment import AttachmentDoc
from ..models.base import utcnow
from .exceptions import InvalidRequest, NotFound, PayloadTooLarge
from . import sessions as sessions_service

# Matches the nginx layers in front of the API (2m) — a bigger body never arrives.
MAX_BYTES = 2 * 1024 * 1024
ORPHAN_TTL = timedelta(days=1)


@dataclass(frozen=True)
class StoredAttachment:
    doc: AttachmentDoc
    data: bytes


def sniff(data: bytes) -> str | None:
    """The image type from the bytes themselves — a client's Content-Type is a claim."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


async def store(user_id: str, session_id: str, data: bytes) -> AttachmentDoc:
    """Save an uploaded photo against one of this user's sessions."""
    await sessions_service.ensure_owned(user_id, session_id)
    if not data:
        raise InvalidRequest("empty upload")
    if len(data) > MAX_BYTES:
        raise PayloadTooLarge(f"photo is over the {MAX_BYTES // (1024 * 1024)} MB limit")
    content_type = sniff(data)
    if content_type is None:
        raise InvalidRequest("unsupported image type (use JPEG, PNG, WebP or GIF)")
    doc = AttachmentDoc(
        user_id=user_id, session_id=session_id, content_type=content_type, size=len(data)
    )
    await attachments_db.insert(doc, data)
    # Housekeeping: uploads that never got sent don't pile up.
    await attachments_db.delete_orphans(user_id, utcnow() - ORPHAN_TTL)
    return doc


async def get(user_id: str, session_id: str, attachment_id: str) -> StoredAttachment:
    """A photo's bytes, if it belongs to this user's session — else NotFound
    (404, not 403: don't confirm someone else's id exists)."""
    found = await attachments_db.find(attachment_id)
    if found is None or found[0].user_id != user_id or found[0].session_id != session_id:
        raise NotFound("attachment", attachment_id)
    return StoredAttachment(*found)


async def claim(
    user_id: str, session_id: str, attachment_ids: list[str]
) -> list[StoredAttachment]:
    """Validate the photos a message references: all must be this user's, from this
    session, and not already used by another message. Order is preserved."""
    claimed: list[StoredAttachment] = []
    for attachment_id in dict.fromkeys(attachment_ids):  # ignore accidental repeats
        try:
            stored = await get(user_id, session_id, attachment_id)
        except NotFound:
            raise InvalidRequest(f"unknown attachment {attachment_id!r}") from None
        if stored.doc.message_id is not None:
            raise InvalidRequest(f"attachment {attachment_id!r} was already sent")
        claimed.append(stored)
    return claimed
