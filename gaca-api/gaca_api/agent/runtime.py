"""Composition root for the agent backend — picks the engine, holds instances.

The concrete engine TYPE is still chosen by config (GACA_AGENT_ENGINE) and
loaded dynamically, so no core module names a specific engine (the
engine-quarantine grep stays empty). Each engine package exposes
`agent/<engine>/adapter.py:create(base_url, api_key) -> AgentBackend`.

What is no longer global is *which* agent: every operator brings their own
Hermes box, so `base_url`/`api_key` are per-user (UserDoc.agent_base_url /
agent_api_key), not a single app-wide setting. `backend_for()` replaces the
old single-instance `backend()` with a small cache keyed by credentials, so
two calls for the same user's agent reuse one adapter (and its HTTP client)
instead of opening a new one per request.
"""
from __future__ import annotations

import importlib
from types import ModuleType

from ..config import settings
from ..services.exceptions import AgentNotConfigured
from .base import AgentBackend

_module: ModuleType | None = None
_cache: dict[tuple[str, str], AgentBackend] = {}


def connect() -> None:
    """Resolve the configured engine's adapter module (startup).

    Doesn't build an adapter yet — there's no shared instance anymore. Each
    (base_url, api_key) pair gets its own, built lazily by backend_for().
    """
    global _module
    if not settings.agent_engine:
        raise RuntimeError("GACA_AGENT_ENGINE is unset — set it (see .env.example).")
    _module = importlib.import_module(f"gaca_api.agent.{settings.agent_engine}.adapter")


async def close() -> None:
    """Release every cached backend (shutdown)."""
    global _module, _cache
    for cached in _cache.values():
        aclose = getattr(cached, "aclose", None)
        if aclose is not None:
            await aclose()
    _cache = {}
    _module = None


def backend_for(base_url: str | None, api_key: str | None) -> AgentBackend:
    """The adapter for one user's agent — built once per (base_url, api_key),
    reused after.

    Raises AgentNotConfigured if either is missing — a normal state for a
    user who hasn't submitted their agent yet, not a startup/wiring bug (that
    case is `_module is None`, below, and stays a RuntimeError).
    """
    if _module is None:
        raise RuntimeError("agent runtime not initialized — is the lifespan running?")
    if not base_url or not api_key:
        raise AgentNotConfigured()
    key = (base_url, api_key)
    if key not in _cache:
        _cache[key] = _module.create(base_url, api_key)
    return _cache[key]
