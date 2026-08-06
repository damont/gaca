"""Hermes → GacaEvent translation.

The ONLY module that knows Hermes' raw SSE event shapes. It maps each raw event
dict (as it arrives on `GET /v1/runs/{id}/events`) onto a neutral GacaEvent. This
is where — and the only where — Hermes' event vocabulary is allowed to appear.

Raw shapes, confirmed live against hermes-agent 0.18.0:

    {"event":"message.delta",       "run_id":…, "timestamp":…, "delta":"…"}
    {"event":"reasoning.available", "run_id":…, "timestamp":…, "text":"…"}
    {"event":"tool.started",        "run_id":…, "timestamp":…, "tool":"…", "preview":"…"}
    {"event":"tool.completed",      "run_id":…, "timestamp":…, "tool":"…", "duration":…, "error":false}
    {"event":"run.completed",       "run_id":…, "timestamp":…, "output":"…", "usage":{…}}
    {"event":"run.failed",          "run_id":…, "timestamp":…, "error":"…"}

Notes from the live capture that shape the mapping:
  * There is no run.started / assistant.completed / done event — run.completed
    (or run.failed) is the terminus.
  * tool.completed carries timing + an error flag but NOT the tool's output.
  * `preview` on tool.started is the arg rendering; we surface it as args_preview.
"""
from __future__ import annotations

from datetime import datetime, timezone

from ...schemas.event import (
    AssistantDelta,
    AssistantReasoning,
    GacaEvent,
    RunCompleted,
    RunFailed,
    ToolCompleted,
    ToolStarted,
    Usage,
)


def _ts(raw: dict) -> datetime | None:
    """Hermes sends epoch-seconds as a float; convert to an aware UTC datetime.

    Preserving the engine's own timestamp (rather than stamping 'now') keeps the
    event's real ordering/timing intact through translation.
    """
    ts = raw.get("timestamp")
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def translate(raw: dict, *, session_id: str, run_id: str) -> GacaEvent | None:
    """Map one raw Hermes event to a neutral GacaEvent.

    Returns None for any event we don't model — unknown events are dropped, never
    passed through raw (that would leak Hermes' shapes downstream). `session_id`
    and `run_id` are *our* ids, supplied by the adapter; `seq` stays None because
    the core stamps it when the event is persisted and published.
    """
    kind = raw.get("event")

    common: dict = {"session_id": session_id, "run_id": run_id}
    created_at = _ts(raw)
    if created_at is not None:
        common["created_at"] = created_at

    if kind == "message.delta":
        return AssistantDelta(**common, text=raw.get("delta", ""))

    if kind == "reasoning.available":
        return AssistantReasoning(**common, text=raw.get("text", ""))

    if kind == "tool.started":
        return ToolStarted(
            **common,
            tool=raw.get("tool", ""),
            args_preview=raw.get("preview"),
        )

    if kind == "tool.completed":
        return ToolCompleted(
            **common,
            tool=raw.get("tool", ""),
            duration=raw.get("duration"),
            error=bool(raw.get("error", False)),
        )

    if kind == "run.completed":
        usage = raw.get("usage")
        return RunCompleted(
            **common,
            output=raw.get("output", ""),
            usage=Usage(**usage) if usage else None,
        )

    if kind == "run.failed":
        return RunFailed(**common, error=raw.get("error", ""))

    return None
