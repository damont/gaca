"""Shutdown must be clean even after a push has been sent.

firebase_admin's cleanup calls asyncio.run() once its messaging client exists (i.e. after the
first push in a process). From inside the server's running event loop that raises, and the
old lifespan then skipped every remaining shutdown step, including closing the database.
"""
import asyncio

import pytest
from fastapi.testclient import TestClient

from gaca_api import main
from gaca_api.config import settings
from gaca_api.db import client
from gaca_api.services import push as push_service


def firebase_style_delete(app):
    """What firebase_admin.delete_app does once a messaging client exists."""
    asyncio.run(asyncio.sleep(0))


def test_push_close_works_from_inside_a_running_event_loop(monkeypatch):
    monkeypatch.setattr(push_service, "_app", object())
    monkeypatch.setattr(push_service.firebase_admin, "delete_app", firebase_style_delete)

    async def shut_down():
        await push_service.aclose()

    asyncio.run(shut_down())  # raised "asyncio.run() cannot be called from a running event loop"
    assert push_service._app is None


def test_push_aclose_is_a_no_op_when_push_is_not_configured(monkeypatch):
    monkeypatch.setattr(push_service, "_app", None)
    asyncio.run(push_service.aclose())


def test_one_failing_shutdown_step_does_not_skip_closing_the_database(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "t.db"))
    monkeypatch.setattr(settings, "agent_engine", "hermes")

    async def broken():
        raise RuntimeError("firebase exploded")
    monkeypatch.setattr(push_service, "aclose", broken)

    with TestClient(main.app):
        pass  # leaving the block runs the lifespan shutdown; it must not raise

    with pytest.raises(RuntimeError, match="not initialized"):
        client.connection()  # the database was still closed
