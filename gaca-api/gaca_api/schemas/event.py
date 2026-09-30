"""Neutral streaming events — the GacaEvent contract.

A GacaEvent is one "this just happened" notice emitted while an agent run is in
flight. These types are the engine-neutral vocabulary the whole system speaks:
the services, the SSE transport, and the Android app all code against them, and
none of them knows which agent engine is running underneath.

Whatever engine backs a run emits its own raw events in its own shapes. A
translator living in that engine's adapter package (`agent/<engine>/`) is the
*only* code that knows both vocabularies and maps raw engine events onto these
classes. Swapping engines means writing a new translator — this file never
changes.

Keep it that way: nothing engine-specific belongs in this module — no vendor
name, no vendor event string, no reference to a particular backend. If you need
to record "which raw event does this come from," that note lives in the
translator, not here.

GacaEvent is a *discriminated union*: every variant carries the common envelope
fields (`type`, `seq`, `session_id`, `run_id`, `created_at`) plus its own typed
payload. Clients switch on `.type` and get exactly the right fields.

`seq` is the monotonic-per-session sequence number that drives Last-Event-ID
resume. The translator leaves it None; the *core* (service layer) stamps it when
the event is persisted and published — sequencing is an invariant-core job. So a
freshly translated event has seq=None; a persisted/emitted one has an int.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Usage(BaseModel):
    """Token accounting reported when a run completes."""
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


class _EventBase(BaseModel):
    """The envelope every GacaEvent carries, regardless of type."""
    model_config = ConfigDict(from_attributes=True)

    # Stamped by the core when the event is persisted/published (Last-Event-ID).
    # None on a freshly translated event.
    seq: int | None = None
    session_id: str
    # The run this event belongs to (our run id). None for session-level events.
    run_id: str | None = None
    created_at: datetime = Field(default_factory=_now)


class AssistantDelta(_EventBase):
    """A chunk of the assistant's streamed reply.

    Every delta is its own event — that is what makes the reply render live. The
    full text is reassembled from the deltas and finalized as one durable
    Message when the run completes.
    """
    type: Literal["assistant.delta"] = "assistant.delta"
    text: str


class AssistantReasoning(_EventBase):
    """A thinking/reasoning trace.

    Streamed live so the client can show a "thinking" line, but deliberately
    *not* folded into the durable assistant Message.
    """
    type: Literal["assistant.reasoning"] = "assistant.reasoning"
    text: str


class ToolStarted(_EventBase):
    """The agent began a tool call.

    `args_preview` is a short, human-readable rendering of the call's arguments,
    when the engine provides one.
    """
    type: Literal["tool.started"] = "tool.started"
    tool: str
    args_preview: str | None = None


class ToolCompleted(_EventBase):
    """A tool call finished — carries timing and a success/error flag.

    This event does *not* carry the tool's output, only that it ran. If the
    audit log needs outputs, fetch them from the session transcript separately.
    """
    type: Literal["tool.completed"] = "tool.completed"
    tool: str
    duration: float | None = None
    error: bool = False


class RunCompleted(_EventBase):
    """The run finished successfully — a terminus of the stream.

    `output` is the full assembled assistant text; the core uses it to finalize
    the durable assistant Message. There is no separate "done" event — this (or
    RunFailed) IS the end of the stream.
    """
    type: Literal["run.completed"] = "run.completed"
    output: str
    usage: Usage | None = None


class RunFailed(_EventBase):
    """The run ended in error — also a terminus of the stream."""
    type: Literal["run.failed"] = "run.failed"
    error: str


class RunHeartbeat(_EventBase):
    """The run is still in flight but has been quiet for a while.

    Synthesized by the core (read_run), never by an engine, and EPHEMERAL: it is
    published to open streams but not persisted, so it takes no seq, is not
    replayed on reconnect, and never grows the durable log. Clients that don't
    know it can ignore it. `idle_seconds` is how long the run has gone without a
    real event.
    """
    type: Literal["run.heartbeat"] = "run.heartbeat"
    idle_seconds: float


# The neutral contract the app and services code against. Switch on `.type`.
#
# Reserved for later slices (define when we build them, not before):
#   approval.request  — human-in-the-loop tool gating
#   attachment.added  — an image/file was produced; references a stored blob
#   run.started       — a run-began marker (the core may synthesize one)
GacaEvent = Annotated[
    Union[
        AssistantDelta,
        AssistantReasoning,
        ToolStarted,
        ToolCompleted,
        RunCompleted,
        RunFailed,
        RunHeartbeat,
    ],
    Field(discriminator="type"),
]

# Because GacaEvent is a union alias (not a class), parse/validate through this
# adapter: GacaEventAdapter.validate_json(raw) picks the right variant off
# `type`. The persist + SSE layers use it to round-trip events to/from storage
# and the wire.
GacaEventAdapter: TypeAdapter[GacaEvent] = TypeAdapter(GacaEvent)
