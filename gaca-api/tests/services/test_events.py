"""Tests for the SSE stream service: replay from the log, then live off the bus."""
import asyncio
import json

from gaca_api.bus import event_bus
from gaca_api.config import settings
from gaca_api.db import client
from gaca_api.db import events as events_db
from gaca_api.schemas.event import AssistantDelta
from gaca_api.services import events as events_service


def _delta(text: str) -> AssistantDelta:
    return AssistantDelta(session_id="s1", run_id="r", text=text)


def test_stream_replays_then_goes_live(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def main():
        await client.connect()
        try:
            # two events already persisted (seq 1, 2)
            await events_db.insert(_delta("a"))
            await events_db.insert(_delta("b"))

            gen = events_service.stream("s1", 0)
            out = [await gen.__anext__(), await gen.__anext__()]  # replay

            # now publish a live event (persist to get seq 3, then publish)
            e3 = _delta("c")
            e3.seq = await events_db.insert(e3)
            await event_bus.publish("s1", e3.model_dump_json())

            out.append(await asyncio.wait_for(gen.__anext__(), 1))  # live
            await gen.aclose()
            return out
        finally:
            await client.close()

    out = asyncio.run(main())
    assert [d["event"] for d in out] == ["assistant.delta"] * 3
    assert [d["id"] for d in out] == ["1", "2", "3"]
    assert [json.loads(d["data"])["text"] for d in out] == ["a", "b", "c"]


def test_stream_resumes_after_last_event_id(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def main():
        await client.connect()
        try:
            for text in ("a", "b", "c"):  # seq 1, 2, 3
                await events_db.insert(_delta(text))

            gen = events_service.stream("s1", 2)  # client already saw up to seq 2
            first = await asyncio.wait_for(gen.__anext__(), 1)
            await gen.aclose()
            return first
        finally:
            await client.close()

    first = asyncio.run(main())
    assert first["id"] == "3"
    assert json.loads(first["data"])["text"] == "c"


def test_live_event_deduped_against_replay(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def main():
        await client.connect()
        try:
            e1 = _delta("a")
            e1.seq = await events_db.insert(e1)  # seq 1

            gen = events_service.stream("s1", 0)
            replayed = await gen.__anext__()  # seq 1 from replay

            # re-publish seq 1 (as if it raced in during replay) + a fresh seq 2
            await event_bus.publish("s1", e1.model_dump_json())
            e2 = _delta("b")
            e2.seq = await events_db.insert(e2)
            await event_bus.publish("s1", e2.model_dump_json())

            live = await asyncio.wait_for(gen.__anext__(), 1)  # must skip dup seq 1
            await gen.aclose()
            return replayed, live
        finally:
            await client.close()

    replayed, live = asyncio.run(main())
    assert replayed["id"] == "1"
    assert live["id"] == "2"  # the duplicate seq-1 was dropped, not re-yielded
