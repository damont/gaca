"""Run orchestration.

`start_turn` turns a posted user message into an agent run (non-blocking, returns
202-fast). `read_run` is the background task that drains a run's events into the
durable log and the live bus, and finalizes the assistant message when the run
completes. This is the invariant core: it works the same for any engine — it only
ever sees neutral GacaEvents.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from ..agent.base import AgentBackend
from ..bus import event_bus
from ..db import events as events_db
from ..db import messages as messages_db
from ..db import sessions as sessions_db
from ..models.message import MessageDoc
from ..schemas.event import GacaEvent, RunCompleted, RunFailed
from ..schemas.message import MessageOut
from .exceptions import NotFound

# Strong refs to in-flight reader tasks so they aren't garbage-collected mid-run.
_background: set[asyncio.Task] = set()

OnSettled = Callable[[GacaEvent], Awaitable[None]]


async def start_turn(
    backend: AgentBackend,
    session_id: str,
    content: str,
    *,
    on_settled: OnSettled | None = None,
) -> MessageDoc:
    """Persist the user message, start a run, spawn its reader. Non-blocking.

    `backend` is resolved by the caller (agent/runtime.py:backend_for, keyed
    on the calling user's own credentials) and handed in rather than looked
    up here — this module stays ignorant of users and credentials entirely,
    same as it was engine-ignorant before per-user agents existed.

    History (Option A) is the turns *before* this one, loaded from our SQLite;
    the new message is sent as the user turn. The reader task carries the run's
    events back to the client asynchronously.

    `on_settled`, if given, is awaited once with the terminal event
    (`RunCompleted` or `RunFailed`) when the run ends. It exists so a caller
    outside the chat feature — alert delegation, so far — can react to a run's
    outcome without this invariant core knowing what an alert is.
    """
    if not await sessions_db.exists(session_id):
        raise NotFound("session", session_id)

    prior = await messages_db.find_by_session(session_id)  # history = turns so far
    user_message = MessageDoc(session_id=session_id, role="user", content=content)
    await messages_db.insert(user_message)

    history = [MessageOut.model_validate(m) for m in prior]
    run_id = await backend.start_run(
        session_id=session_id, history=history, user_message=content
    )

    task = asyncio.create_task(
        read_run(backend, session_id, run_id, on_settled=on_settled)
    )
    _background.add(task)
    task.add_done_callback(_background.discard)
    return user_message


async def read_run(
    backend: AgentBackend,
    session_id: str,
    run_id: str,
    *,
    on_settled: OnSettled | None = None,
) -> None:
    """Drain a run's events: persist (stamps seq), publish live, finalize on end."""
    async for event in backend.stream_events(run_id, session_id=session_id):
        # Persist FIRST — the insert assigns seq (Last-Event-ID) atomically.
        event.seq = await events_db.insert(event)
        # Then publish the seq-stamped event to any open stream (ephemeral).
        await event_bus.publish(session_id, event.model_dump_json())

        # On completion, finalize the assistant turn into the durable transcript
        # so the next turn's history replay includes it.
        if isinstance(event, RunCompleted):
            assistant = MessageDoc(
                session_id=session_id, role="assistant", content=event.output
            )
            await messages_db.insert(assistant)

        if on_settled is not None and isinstance(event, (RunCompleted, RunFailed)):
            await on_settled(event)
