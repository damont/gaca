"""Opt-in live test: drives the REAL Hermes gateway on :8642.

Skipped by default. It only runs when GACA_LIVE_HERMES=1, so a plain `pytest`
never touches Hermes. It reads the gateway bearer from ~/.hermes/.env.

Run it yourself with:

    GACA_LIVE_HERMES=1 .venv/bin/pytest tests/agent/test_adapter_live.py -s
"""
import asyncio
import os
import pathlib

import pytest

from gaca_api.agent.hermes.adapter import HermesAdapter

pytestmark = pytest.mark.skipif(
    os.environ.get("GACA_LIVE_HERMES") != "1",
    reason="set GACA_LIVE_HERMES=1 to run against the live gateway",
)


def _gateway_key() -> str:
    env = pathlib.Path.home() / ".hermes" / ".env"
    if not env.exists():
        pytest.skip("~/.hermes/.env not found")
    for line in env.read_text().splitlines():
        if line.startswith("API_SERVER_KEY="):
            return line.split("=", 1)[1].strip()
    pytest.skip("API_SERVER_KEY not set in ~/.hermes/.env")
    return ""  # unreachable, keeps type checkers happy


def test_live_round_trip():
    key = _gateway_key()

    async def run():
        adapter = HermesAdapter("http://127.0.0.1:8642", key)
        try:
            run_id = await adapter.start_run(
                session_id="adapter-live-1",
                history=[],
                user_message="Say hi in exactly three words.",
            )
            assert run_id, "expected a run_id back from /v1/runs"

            types = []
            async for event in adapter.stream_events(run_id, session_id="adapter-live-1"):
                types.append(event.type)
                print(f"  {event.type}")

            # The stream must terminate on a real completion.
            assert "run.completed" in types, f"no run.completed in {types}"
        finally:
            await adapter.aclose()

    asyncio.run(run())
