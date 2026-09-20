"""Tests for the session service: ownership scoping.

Sessions predate multi-user auth (the original chat feature had no user
concept at all) — this locks in the retrofit: a session belongs to whoever
created it, and nobody else's calls can see or touch it.
"""
import asyncio

import pytest

from gaca_api.config import settings
from gaca_api.db import client
from gaca_api.services import sessions as session_service
from gaca_api.services.exceptions import NotFound


def _run(factory):
    async def main():
        await client.connect()
        try:
            return await factory()
        finally:
            await client.close()

    return asyncio.run(main())


def test_create_stamps_the_owner(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        return await session_service.create("u1", "dev session", "hermes")

    session = _run(go)
    assert session.user_id == "u1"
    assert session.title == "dev session"
    assert session.crew == "hermes"
    assert session.alert_id is None


def test_create_stamps_the_originating_alert(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        return await session_service.create("u1", None, "hermes", alert_id="a1")

    session = _run(go)
    assert session.alert_id == "a1"


def test_ensure_owned_returns_the_session_for_its_owner(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        created = await session_service.create("u1", None, "hermes")
        return created, await session_service.ensure_owned("u1", created.id)

    created, found = _run(go)
    assert found.id == created.id


def test_ensure_owned_raises_not_found_for_a_different_user(tmp_path):
    """404, not 403 — same reasoning as alerts: a 403 would confirm the id
    exists, which someone else's caller has no business learning."""
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        created = await session_service.create("u1", None, "hermes")
        with pytest.raises(NotFound):
            await session_service.ensure_owned("u2", created.id)

    _run(go)


def test_ensure_owned_raises_not_found_for_an_unknown_session(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        with pytest.raises(NotFound):
            await session_service.ensure_owned("u1", "nope")

    _run(go)


def test_list_for_user_is_newest_first_and_scoped_to_the_owner(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        await session_service.create("u1", "first", "hermes")
        await session_service.create("u2", "someone else's", "hermes")
        await session_service.create("u1", "second", "hermes")
        return await session_service.list_for_user("u1")

    sessions = _run(go)
    assert [s.title for s in sessions] == ["second", "first"]


def test_get_with_messages_is_scoped_to_the_owner(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        created = await session_service.create("u1", None, "hermes")
        with pytest.raises(NotFound):
            await session_service.get_with_messages("u2", created.id)
        session, messages = await session_service.get_with_messages("u1", created.id)
        return session, messages

    session, messages = _run(go)
    assert session.user_id == "u1"
    assert messages == []
