"""MongoDB connection — Mongo is the system of record.

One client for the whole process, opened in the app's lifespan and reused by
every request. `database()` hands callers the database handle.
"""
from __future__ import annotations

from pymongo import AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase

from ..config import settings

_client: AsyncMongoClient | None = None


def connect() -> None:
    """Create the shared client (called once from the lifespan on startup).

    AsyncMongoClient connects lazily — the first query establishes the socket.
    """
    global _client
    _client = AsyncMongoClient(settings.mongo_url)


async def close() -> None:
    """Close the shared client on shutdown."""
    global _client
    if _client is not None:
        await _client.close()
        _client = None


def database() -> AsyncDatabase:
    """The database handle. Raises if used before the lifespan ran."""
    if _client is None:
        raise RuntimeError("Mongo client not initialized — is the lifespan running?")
    return _client[settings.mongo_db]
