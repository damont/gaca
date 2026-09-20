"""Offline tests for HermesAdapter, using httpx.MockTransport as a fake gateway.

No network, no live Hermes — the transport answers /v1/runs and the SSE endpoint
with canned responses so we can assert the request the adapter builds and the
events it yields.
"""
import asyncio
import json

import httpx
import pytest

from gaca_api.agent.base import AgentBackend
from gaca_api.agent.hermes.adapter import HermesAdapter
from gaca_api.schemas.event import AssistantDelta, RunCompleted, ToolStarted
from gaca_api.schemas.message import MessageOut

SSE_BODY = (
    'data: {"event":"message.delta","run_id":"run_abc","timestamp":1783194662.07,"delta":"Hi"}\n'
    "\n"
    'data: {"event":"tool.started","run_id":"run_abc","timestamp":1783194663.0,"tool":"memory","preview":"x"}\n'
    "\n"
    'data: {"event":"run.completed","run_id":"run_abc","timestamp":1783194672.84,"output":"Hi",'
    '"usage":{"input_tokens":10,"output_tokens":2,"total_tokens":12}}\n'
    "\n"
    ": stream closed\n"
)


def _make_adapter(captured=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/runs" and request.method == "POST":
            if captured is not None:
                captured["auth"] = request.headers.get("authorization")
                captured["payload"] = json.loads(request.content)
            return httpx.Response(200, json={"run_id": "run_abc", "status": "started"})
        if request.url.path == "/v1/runs/run_abc/events":
            return httpx.Response(
                200, text=SSE_BODY, headers={"content-type": "text/event-stream"}
            )
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return HermesAdapter("http://engine:8642", "supersecretkey123456", client=client)


def test_adapter_is_agentbackend():
    assert isinstance(_make_adapter(), AgentBackend)


def test_start_run_builds_option_a_payload():
    captured: dict = {}
    adapter = _make_adapter(captured)
    history = [
        MessageOut(id="m1", session_id="s", role="user", content="Salmon?",
                   created_at="2026-07-04T00:00:00Z"),
        MessageOut(id="m2", session_id="s", role="assistant", content="Five species.",
                   created_at="2026-07-04T00:00:01Z"),
    ]

    async def run():
        return await adapter.start_run(session_id="s", history=history, user_message="Near Haines?")

    run_id = asyncio.run(run())
    assert run_id == "run_abc"
    assert captured["auth"] == "Bearer supersecretkey123456"
    assert captured["payload"]["input"] == "Near Haines?"
    assert captured["payload"]["session_id"] == "s"
    assert captured["payload"]["conversation_history"] == [
        {"role": "user", "content": "Salmon?"},
        {"role": "assistant", "content": "Five species."},
    ]


def test_stream_events_translates_and_skips_noise():
    adapter = _make_adapter()

    async def collect():
        return [e async for e in adapter.stream_events("run_abc", session_id="s")]

    events = asyncio.run(collect())
    assert [type(e) for e in events] == [AssistantDelta, ToolStarted, RunCompleted]
    assert all(e.session_id == "s" and e.run_id == "run_abc" for e in events)
    assert events[-1].usage.total_tokens == 12


def test_start_run_without_key_omits_auth_header():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"run_id": "run_abc"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = HermesAdapter("http://engine:8642", "", client=client)  # empty key

    async def run():
        return await adapter.start_run(session_id="s", history=[], user_message="hi")

    asyncio.run(run())
    assert captured["auth"] is None  # no malformed "Bearer " header sent


def test_stream_events_idle_timeout_yields_run_failed():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/runs/run_abc/events":
            raise httpx.ReadTimeout("idle", request=request)
        return httpx.Response(200, json={"run_id": "run_abc", "status": "started"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = HermesAdapter(
        "http://engine:8642", "supersecretkey123456", stream_idle_timeout=30.0, client=client
    )

    async def collect():
        return [e async for e in adapter.stream_events("run_abc", session_id="s")]

    events = asyncio.run(collect())
    assert [type(e).__name__ for e in events] == ["RunFailed"]
    assert "30" in events[0].error  # the idle window is reported


def test_stream_events_http_error_yields_run_failed():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/runs/run_abc/events":
            return httpx.Response(404, json={"error": "no such run"})
        return httpx.Response(200, json={"run_id": "run_abc"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = HermesAdapter("http://engine:8642", "supersecretkey123456", client=client)

    async def collect():
        return [e async for e in adapter.stream_events("run_abc", session_id="s")]

    events = asyncio.run(collect())
    assert [type(e).__name__ for e in events] == ["RunFailed"]


def test_reserved_methods_not_implemented():
    adapter = _make_adapter()

    async def call():
        await adapter.set_model(None)

    with pytest.raises(NotImplementedError):
        asyncio.run(call())


def test_list_skills_maps_hermes_v1_skills():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/skills"
        assert request.method == "GET"
        return httpx.Response(200, json={
            "object": "list",
            "data": [
                {"name": "email", "description": "Reads and drafts Gmail.", "category": "productivity"},
                {"name": "research", "description": "Runs web research.", "category": "research"},
            ],
        })

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = HermesAdapter("http://engine:8642", "supersecretkey123456", client=client)

    skills = asyncio.run(adapter.list_skills())
    assert [s.name for s in skills] == ["email", "research"]
    assert skills[0].category == "productivity"
