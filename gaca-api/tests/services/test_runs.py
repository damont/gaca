"""Tests for run orchestration (start_turn + read_run), offline via a stub backend.

No Hermes contact: we inject a StubBackend that yields canned GacaEvents, so this
exercises the invariant-core wiring (persist, seq-stamp, publish, finalize).
`runs.py` takes the backend as a plain argument now — no agent_runtime
monkeypatching needed, it's just a value passed in like any other.
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

import gaca_api.services.runs as runs
from gaca_api.config import settings
from gaca_api.db import client
from gaca_api.db import events as events_db
from gaca_api.db import messages as messages_db
from gaca_api.db import sessions as sessions_db
from gaca_api.models.message import MessageDoc, ToolUsage
from gaca_api.models.session import SessionDoc
from gaca_api.schemas.event import AssistantDelta, RunCompleted, ToolCompleted, ToolStarted
from gaca_api.schemas.message import MessageOut
from gaca_api.services.exceptions import NotFound


class StubBackend:
    def __init__(self) -> None:
        self.last_history = None
        self.last_user = None

    async def start_run(self, *, session_id, history, user_message, images=()) -> str:
        self.last_images = list(images)
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
    stub = StubBackend()
    recorder = RecorderBus()
    monkeypatch.setattr(runs, "event_bus", recorder)

    async def main():
        await client.connect()
        try:
            await sessions_db.insert(SessionDoc(id="s1", user_id="u1"))
            await runs.read_run(stub, "s1", "run_stub")
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


def test_start_turn_sends_prior_history_and_finalizes(tmp_path):
    settings.db_path = str(tmp_path / "t.db")
    stub = StubBackend()

    async def main():
        await client.connect()
        try:
            await sessions_db.insert(SessionDoc(id="s1", user_id="u1"))
            # seed two prior turns with fixed early timestamps for stable ordering
            await messages_db.insert(MessageDoc(
                session_id="s1", role="user", content="earlier",
                created_at=datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)))
            await messages_db.insert(MessageDoc(
                session_id="s1", role="assistant", content="prior-reply",
                created_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc)))

            user_msg = await runs.start_turn(stub, "s1", "new question")
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


def test_start_turn_unknown_session_raises(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def main():
        await client.connect()
        try:
            with pytest.raises(NotFound):
                await runs.start_turn(StubBackend(), "nope", "hi")
        finally:
            await client.close()

    asyncio.run(main())


class SlowBackend(StubBackend):
    """Goes quiet for a while between events, like an engine mid model-call."""

    async def stream_events(self, run_id, *, session_id):
        yield AssistantDelta(session_id=session_id, run_id=run_id, text="Hi")
        await asyncio.sleep(0.35)
        yield RunCompleted(session_id=session_id, run_id=run_id, output="Hi there")


def test_read_run_heartbeats_while_quiet_without_persisting_them(tmp_path, monkeypatch):
    settings.db_path = str(tmp_path / "t.db")
    recorder = RecorderBus()
    monkeypatch.setattr(runs, "event_bus", recorder)
    monkeypatch.setattr(runs, "HEARTBEAT_INTERVAL", 0.1)

    async def main():
        await client.connect()
        try:
            await sessions_db.insert(SessionDoc(id="s1", user_id="u1"))
            await runs.read_run(SlowBackend(), "s1", "run_slow")
            return await events_db.find_by_session("s1")
        finally:
            await client.close()

    stored = asyncio.run(main())

    types = [json.loads(m)["type"] for _, m in recorder.messages]
    assert types[0] == "assistant.delta" and types[-1] == "run.completed"
    assert types.count("run.heartbeat") >= 2  # ~0.35s quiet at 0.1s cadence
    # ephemeral: not in the durable log, and the stream still ran to completion
    assert [e.type for e in stored] == ["assistant.delta", "run.completed"]
    beat = next(json.loads(m) for _, m in recorder.messages if '"run.heartbeat"' in m)
    assert beat["seq"] is None and beat["idle_seconds"] >= 0


def test_chat_reply_notifier_pushes_reply_and_failure(monkeypatch):
    from gaca_api.schemas.event import RunFailed
    from gaca_api.services import push as push_service

    pushed = []

    async def fake_notify_chat(user_id, *, title, body, session_id):
        pushed.append((user_id, title, body, session_id))

    monkeypatch.setattr(push_service, "notify_chat", fake_notify_chat)
    notify = runs.chat_reply_notifier("u1", "s1", "Hermes")

    async def main():
        await notify(RunCompleted(session_id="s1", run_id="r", output="**Done** — moved it to Friday.\n\nAnything else?"))
        await notify(RunFailed(session_id="s1", run_id="r", error="engine event stream idle for over 180s"))
        await notify(AssistantDelta(session_id="s1", run_id="r", text="ignored"))

    asyncio.run(main())
    assert pushed == [
        ("u1", "Hermes", "Done — moved it to Friday. Anything else?", "s1"),
        ("u1", "Hermes couldn't finish", "engine event stream idle for over 180s", "s1"),
    ]


# --- activity: what the run did, stored on the assistant message -------------

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


class ToolBackend(StubBackend):
    """A run that used two tools (one of them three times, once failing)."""

    async def stream_events(self, run_id, *, session_id):
        def tool(tool, error=None):
            if error is None:
                return ToolStarted(session_id=session_id, run_id=run_id, tool=tool)
            return ToolCompleted(session_id=session_id, run_id=run_id, tool=tool, error=error)

        yield tool("web_search")
        yield tool("web_search", error=False)
        for ok in (True, False, True):
            yield tool("terminal")
            yield tool("terminal", error=not ok)
        yield RunCompleted(
            session_id=session_id, run_id=run_id, output="Done",
            created_at=T0 + timedelta(seconds=41.3),
        )


def _finalize(tmp_path, backend):
    settings.db_path = str(tmp_path / "t.db")

    async def main():
        await client.connect()
        try:
            await sessions_db.insert(SessionDoc(id="s1", user_id="u1"))
            await runs.read_run(backend, "s1", "run_x", started_at=T0)
            return await messages_db.find_by_session("s1")
        finally:
            await client.close()

    return asyncio.run(main())


def test_completed_run_stores_its_tool_activity_on_the_reply(tmp_path):
    [reply] = _finalize(tmp_path, ToolBackend())
    activity = reply.activity
    assert [(t.name, t.count, t.errors) for t in activity.tools] == [
        ("web_search", 1, 0),
        ("terminal", 3, 1),
    ]
    assert activity.duration_seconds == 41.3
    assert activity.label == "Used web_search, terminal 3× · 41s"
    # and it reaches the client shape unchanged
    out = MessageOut.model_validate(reply)
    assert out.activity.label == activity.label


class NoToolBackend(StubBackend):
    async def stream_events(self, run_id, *, session_id):
        yield AssistantDelta(session_id=session_id, run_id=run_id, text="Hi")
        yield RunCompleted(session_id=session_id, run_id=run_id, output="Hi")


def test_a_run_without_tools_has_no_activity(tmp_path):
    [reply] = _finalize(tmp_path, NoToolBackend())
    assert reply.activity is None


def test_activity_label_wording():
    use = lambda name, n: ToolUsage(name=name, count=n)  # noqa: E731
    assert runs.activity_label([use("terminal", 3)], 41) == "Ran terminal 3× · 41s"
    assert runs.activity_label([use("terminal", 1)], 8) == "Ran terminal · 8s"
    assert runs.activity_label([use("web_search", 1), use("terminal", 2)], 8.2) == (
        "Used web_search, terminal 2× · 8s"
    )
    assert runs.format_duration(65) == "1m 05s"
    assert runs.format_duration(59.6) == "1m 00s"
    assert runs.format_duration(3720) == "1h 02m"


def test_messages_stored_before_activity_existed_read_back_as_none():
    old = '{"id":"m1","session_id":"s1","role":"assistant","content":"hi","created_at":"2026-01-01T00:00:00Z"}'
    assert MessageDoc.model_validate_json(old).activity is None


# --- in-flight tracking: SessionDetail.running / resume_seq -----------------

class BlockingBackend(StubBackend):
    """Emits one tool event, then holds the run open until `release` is set —
    lets a test look at the session mid-run."""

    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()
        self.started = asyncio.Event()  # set before the first event
        self.emitted = asyncio.Event()  # set once the tool event is out

    async def start_run(self, **kwargs) -> str:
        await super().start_run(**kwargs)
        return "run_blocking"

    async def stream_events(self, run_id, *, session_id):
        self.started.set()
        await self.release.wait()
        self.release.clear()
        yield ToolStarted(session_id=session_id, run_id=run_id, tool="terminal")
        self.emitted.set()
        await self.release.wait()
        yield RunCompleted(session_id=session_id, run_id=run_id, output="Done")


def test_session_detail_reports_the_in_flight_run_and_where_to_resume(tmp_path):
    from gaca_api.services import sessions as session_service

    settings.db_path = str(tmp_path / "t.db")

    async def main():
        await client.connect()
        try:
            s = await session_service.create("u1", None, "hermes")
            # An earlier, finished run: its events are history, not to be replayed.
            await runs.start_turn(StubBackend(), s.id, "first")
            await asyncio.gather(*list(runs._background))
            idle = await session_service.get_detail("u1", s.id)

            backend = BlockingBackend()
            await runs.start_turn(backend, s.id, "second")
            just_posted = await session_service.get_detail("u1", s.id)  # no events yet
            await backend.started.wait()
            backend.release.set()
            await backend.emitted.wait()
            await asyncio.sleep(0.05)  # let read_run persist the tool event
            mid_run = await session_service.get_detail("u1", s.id)

            backend.release.set()
            await asyncio.gather(*list(runs._background))
            after = await session_service.get_detail("u1", s.id)
            return idle, just_posted, mid_run, after
        finally:
            await client.close()

    idle, just_posted, mid_run, after = asyncio.run(main())
    # first run persisted seqs 1-3 (delta, tool, completed)
    assert (idle.running, idle.run_started_at, idle.resume_seq) == (False, None, 3)
    assert just_posted.running and just_posted.run_started_at is not None
    assert just_posted.resume_seq == 3  # nothing from the new run yet
    assert mid_run.running and mid_run.resume_seq == 3  # replay starts at seq 4, the new run's first
    assert (after.running, after.run_started_at, after.resume_seq) == (False, None, 5)
    assert runs.active_run(after.id) is None


def test_a_finished_older_run_does_not_clear_a_newer_one(tmp_path):
    """Two runs in one session can overlap (a second message sent mid-run): the
    first one ending must not mark the session idle while the second goes on."""
    settings.db_path = str(tmp_path / "t.db")
    newer = ("run_newer", datetime.now(timezone.utc))

    async def main():
        await client.connect()
        try:
            await sessions_db.insert(SessionDoc(id="s1", user_id="u1"))
            runs._active["s1"] = newer
            await runs.read_run(StubBackend(), "s1", "run_stub")
            return runs.active_run("s1")
        finally:
            runs._active.pop("s1", None)
            await client.close()

    assert asyncio.run(main()) == newer
