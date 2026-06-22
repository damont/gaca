"""Data access for the `sessions` collection. Returns DB models, not raw dicts."""
from __future__ import annotations

from ..models.session import SessionDoc
from .client import database


async def insert(session: SessionDoc) -> None:
    await database().sessions.insert_one(session.model_dump(by_alias=True))


async def find(session_id: str) -> SessionDoc | None:
    doc = await database().sessions.find_one({"_id": session_id})
    return SessionDoc(**doc) if doc is not None else None


async def exists(session_id: str) -> bool:
    # project just _id — we only care whether it's there
    return await database().sessions.find_one({"_id": session_id}, {"_id": 1}) is not None
