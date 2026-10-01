"""Run orchestration.

`start_turn` turns a posted user message into an agent run (non-blocking, returns
202-fast). `read_run` is the background task that drains a run's events into the
durable log and the live bus, and finalizes the assistant message when the run
completes. This is the invariant core: it works the same for any engine — it only
ever sees neutral GacaEvents.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING
from collections.abc import Awaitable, Callable, Sequence

from ..agent.base import AgentBackend
from ..bus import event_bus
from ..db import events as events_db
from ..db import messages as messages_db
from ..db import sessions as sessions_db
from ..db import attachments as attachments_db
from ..models.message import AttachmentMeta, MessageDoc
from ..schemas.event import GacaEvent, RunCompleted, RunFailed, RunHeartbeat
from ..schemas.message import ImageInput, MessageOut
from .exceptions import NotFound

logger = logging.getLogger(__name__)

# How long a run may go without a real event before open streams get a
# run.heartbeat ("still working"). Kept well under the engine idle timeout.
HEARTBEAT_INTERVAL = 15.0

# Strong refs to in-flight reader tasks so they aren't garbage-collected mid-run.
_background: set[asyncio.Task] = set()

OnSettled = Callable[[GacaEvent], Awaitable[None]]

if TYPE_CHECKING:
    from .attachments import StoredAttachment


async def start_turn(
    backend: AgentBackend,
    session_id: str,
    content: str,
    *,
    on_settled: OnSettled | None = None,
    attachments: Sequence["StoredAttachment"] = (),
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
    user_message = MessageDoc(
        session_id=session_id,
        role="user",
        content=content,
        attachments=[
            AttachmentMeta(id=a.doc.id, content_type=a.doc.content_type, size=a.doc.size)
            for a in attachments
        ],
    )
    await messages_db.insert(user_message)
    if attachments:
        await attachments_db.attach([a.doc.id for a in attachments], user_message.id)

    history = [MessageOut.model_validate(m) for m in prior]
    run_id = await backend.start_run(
        session_id=session_id,
        history=history,
        user_message=content,
        images=[ImageInput(a.doc.content_type, a.data) for a in attachments],
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
    """Drain a run's events: persist (stamps seq), publish live, finalize on end.

    While the engine is quiet, publishes an ephemeral RunHeartbeat every
    HEARTBEAT_INTERVAL so clients can tell "working" from "hung".
    """
    events = backend.stream_events(run_id, session_id=session_id).__aiter__()
    pending: asyncio.Future | None = None
    last_event_at = time.monotonic()
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(events.__anext__())
            # asyncio.wait (not wait_for): a timeout must not cancel the pending
            # read, or we'd tear down the engine stream mid-run.
            done, _ = await asyncio.wait({pending}, timeout=HEARTBEAT_INTERVAL)
            if not done:
                heartbeat = RunHeartbeat(
                    session_id=session_id,
                    run_id=run_id,
                    idle_seconds=round(time.monotonic() - last_event_at, 1),
                )
                await event_bus.publish(session_id, heartbeat.model_dump_json())
                continue
            finished, pending = pending, None
            try:
                event = finished.result()
            except StopAsyncIteration:
                break
            last_event_at = time.monotonic()
            await _handle_event(session_id, run_id, event, on_settled)
    finally:
        if pending is not None:
            pending.cancel()


async def _handle_event(
    session_id: str, run_id: str, event: GacaEvent, on_settled: OnSettled | None
) -> None:
    # Persist FIRST — the insert assigns seq (Last-Event-ID) atomically.
    event.seq = await events_db.insert(event)
    # Then publish the seq-stamped event to any open stream (ephemeral).
    await event_bus.publish(session_id, event.model_dump_json())

    if isinstance(event, RunFailed):
        logger.warning(
            "run failed: session=%s run=%s error=%s", session_id, run_id, event.error
        )

    # On completion, finalize the assistant turn into the durable transcript
    # so the next turn's history replay includes it.
    if isinstance(event, RunCompleted):
        assistant = MessageDoc(
            session_id=session_id, role="assistant", content=event.output
        )
        await messages_db.insert(assistant)

    if on_settled is not None and isinstance(event, (RunCompleted, RunFailed)):
        await on_settled(event)


def chat_reply_notifier(
    user_id: str, session_id: str, crew_name: str | None
) -> OnSettled:
    """An `on_settled` hook that pushes "your agent replied" (or couldn't) for a
    normal chat turn. Alert delegation brings its own hook, which pushes through
    the alert instead — so this is only attached to turns the user typed.

    The body is the reply tidied to one line, same rules as the Chat list
    preview, so what the notification shows matches what the app shows. Push is
    best effort: a failure here never touches the run.
    """
    from . import push as push_service
    from .sessions import PREVIEW_MAX, tidy

    title = crew_name or "Your agent"

    async def notify(event: GacaEvent) -> None:
        if isinstance(event, RunCompleted):
            body = tidy(event.output, PREVIEW_MAX) or "Replied."
            await push_service.notify_chat(
                user_id, title=title, body=body, session_id=session_id
            )
        elif isinstance(event, RunFailed):
            await push_service.notify_chat(
                user_id,
                title=f"{title} couldn't finish",
                body=tidy(event.error, PREVIEW_MAX) or "Something went wrong.",
                session_id=session_id,
            )

    return notify
