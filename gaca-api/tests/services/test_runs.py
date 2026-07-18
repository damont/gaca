"""Tests for run orchestration (start_turn + read_run), offline via a stub backend.

No Hermes contact: we inject a StubBackend that yields canned GacaEvents, so this
exercises the invariant-core wiring (persist, seq-stamp, publish, finalize).
"""
import asyncio
import json
from datetime import datetime, timezone

import pytest

import gaca_api.agent.runtime as agent_runtime
import gaca_api.services.runs as runs
from gaca_api.config import settings
from gaca_api.db import client
from gaca_api.db import events as events_db
from gaca_api.db import messages as messages_db
from gaca_api.db import sessions as sessions_db
from gaca_api.models.message import MessageDoc
from gaca_api.models.session import SessionDoc
from gaca_api.schemas.event import AssistantDelta, RunCompleted, ToolStarted
from gaca_api.services.exceptions import NotFound


class StubBackend:
    def __init__(self) -> None:
        self.last_history = None
        self.last_user = None

    async def start_run(self, *, session_id, history, user_message) -> str:
        self.last_history = [m.content for m in history]
        self.last_user = user_message
        return "run_stub"

    async def stream_events(self, run_id, *, session_id):
        yield AssistantDelta(session_id=session_id, run_id=run_id, text="Hi")
        yield ToolStarted(session_id=session_id, run_id=run_id, tool="memory")
        yield RunCompleted(session_id=session_id, run_id=run_id, output="Hi there")


class RecorderBus:
    """A bus stand-in that just records what was published."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    async def connect(self) -> None: ...
    async def aclose(self) -> None: ...
    async def publish(self, channel: str, message: str) -> None:
        self.messages.append((channel, message))

    def subscribe(self, channel: str):
        raise NotImplementedError


def test_read_run_persists_publishes_and_finalizes(tmp_path, monkeypatch):
    settings.db_path = str(tmp_path / "t.db")
    monkeypatch.setattr(agent_runtime, "_backend", StubBackend())
    recorder = RecorderBus()
    monkeypatch.setattr(runs, "event_bus", recorder)

    async def main():
        await client.connect()
        try:
            await sessions_db.insert(SessionDoc(id="s1"))
            await runs.read_run("s1", "run_stub")
            stored = await events_db.find_by_session("s1")
            msgs = await messages_db.find_by_session("s1")
            return stored, msgs
        finally:
            await client.close()

    stored, msgs = asyncio.run(main())

    # persisted, monotonic seq, neutral types
    assert [e.type for e in stored] == ["assistant.delta", "tool.started", "run.completed"]
    assert [e.seq for e in stored] == [1, 2, 3]

    # published to the session channel, each payload carrying its stamped seq
    assert [ch for ch, _ in recorder.messages] == ["s1", "s1", "s1"]
    assert [json.loads(m)["seq"] for _, m in recorder.messages] == [1, 2, 3]

    # assistant turn finalized from run.completed output
    assert [m.role for m in msgs] == ["assistant"]
    assert msgs[0].content == "Hi there"


def test_start_turn_sends_prior_history_and_finalizes(tmp_path, monkeypatch):
    settings.db_path = str(tmp_path / "t.db")
    stub = StubBackend()
    monkeypatch.setattr(agent_runtime, "_backend", stub)

    async def main():
        await client.connect()
        try:
            await sessions_db.insert(SessionDoc(id="s1"))
            # seed two prior turns with fixed early timestamps for stable ordering
            await messages_db.insert(MessageDoc(
                session_id="s1", role="user", content="earlier",
                created_at=datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)))
            await messages_db.insert(MessageDoc(
                session_id="s1", role="assistant", content="prior-reply",
                created_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc)))

            user_msg = await runs.start_turn("s1", "new question")
            await asyncio.gather(*list(runs._background))  # let the reader finish

            msgs = await messages_db.find_by_session("s1")
            return user_msg, msgs
        finally:
            await client.close()

    user_msg, msgs = asyncio.run(main())
    assert user_msg.role == "user" and user_msg.content == "new question"
    # history sent to the engine was the PRIOR turns, not the new message (Option A)
    assert stub.last_history == ["earlier", "prior-reply"]
    assert stub.last_user == "new question"
    # transcript now includes the finalized assistant reply, in order
    assert [m.content for m in msgs] == ["earlier", "prior-reply", "new question", "Hi there"]


def test_start_turn_unknown_session_raises(tmp_path, monkeypatch):
    settings.db_path = str(tmp_path / "t.db")
    monkeypatch.setattr(agent_runtime, "_backend", StubBackend())

    async def main():
        await client.connect()
        try:
            with pytest.raises(NotFound):
                await runs.start_turn("nope", "hi")
        finally:
            await client.close()

    asyncio.run(main())
