"""Data access for the `messages` collection."""
from __future__ import annotations

from datetime import datetime

from ..models.message import MessageDoc
from .client import database


async def insert(message: MessageDoc) -> None:
    await database().messages.insert_one(message.model_dump(by_alias=True))


async def find_by_session(
    session_id: str, after: datetime | None = None
) -> list[MessageDoc]:
    """Messages for a session, oldest first.

    `after` is a cursor: when given, only messages created strictly after that
    timestamp are returned (used by the SSE poll loop to fetch just new ones).
    """
    query: dict = {"session_id": session_id}
    if after is not None:
        query["created_at"] = {"$gt": after}
    cursor = database().messages.find(query).sort("created_at")
    return [MessageDoc(**m) async for m in cursor]
