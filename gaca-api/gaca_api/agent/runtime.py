"""Composition root for the agent backend — picks the engine, holds the instance.

The concrete engine is chosen by config (GACA_AGENT_ENGINE) and loaded
dynamically, so no core module names a specific engine (the engine-quarantine
grep stays empty). Each engine package exposes
`agent/<engine>/adapter.py:create() -> AgentBackend`.

Lifecycle mirrors db.client: `connect()` on startup builds the backend, `close()`
on shutdown releases it, `backend()` hands callers the shared instance.
"""
from __future__ import annotations

import importlib

from ..config import settings
from .base import AgentBackend

_backend: AgentBackend | None = None


def connect() -> None:
    """Instantiate the configured backend (startup)."""
    global _backend
    if not settings.agent_engine:
        raise RuntimeError("GACA_AGENT_ENGINE is unset — set it (see .env.example).")
    module = importlib.import_module(f"gaca_api.agent.{settings.agent_engine}.adapter")
    _backend = module.create()


async def close() -> None:
    """Release the backend on shutdown (closes its HTTP client, etc.)."""
    global _backend
    if _backend is not None:
        aclose = getattr(_backend, "aclose", None)
        if aclose is not None:
            await aclose()
    _backend = None


def backend() -> AgentBackend:
    """The shared backend. Raises if used before the lifespan ran."""
    if _backend is None:
        raise RuntimeError("agent backend not initialized — is the lifespan running?")
    return _backend
