"""Tests for the session service: ownership scoping.

Sessions predate multi-user auth (the original chat feature had no user
concept at all) — this locks in the retrofit: a session belongs to whoever
created it, and nobody else's calls can see or touch it.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from gaca_api.config import settings
from gaca_api.db import client
from gaca_api.db import messages as messages_db
from gaca_api.models.message import MessageDoc
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


def test_list_for_user_orders_by_creation_when_nothing_has_messages_yet(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        await session_service.create("u1", "first", "hermes")
        await session_service.create("u2", "someone else's", "hermes")
        await session_service.create("u1", "second", "hermes")
        return await session_service.list_for_user("u1")

    sessions = _run(go)
    assert [s.title for s in sessions] == ["second", "first"]


def test_list_for_user_orders_by_most_recent_message_not_creation(tmp_path):
    """A months-old session that just got a reply outranks one created much
    more recently but never actually talked to."""
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        old_session = await session_service.create("u1", "old but active", "hermes")
        await session_service.create("u1", "new but quiet", "hermes")
        # Safely after both sessions' own created_at, so the reply is what
        # puts "old but active" on top — not an accident of insertion order.
        reply_time = datetime.now(timezone.utc) + timedelta(days=1)
        await messages_db.insert(
            MessageDoc(
                session_id=old_session.id,
                role="user",
                content="hi",
                created_at=reply_time,
            )
        )
        return await session_service.list_for_user("u1")

    sessions = _run(go)
    assert [s.title for s in sessions] == ["old but active", "new but quiet"]


def test_list_for_user_uses_the_latest_message_when_there_are_several(tmp_path):
    settings.db_path = str(tmp_path / "t.db")
    now = datetime.now(timezone.utc)

    async def go():
        active = await session_service.create("u1", "active", "hermes")
        quiet = await session_service.create("u1", "quiet", "hermes")
        # "quiet" has an older message than "active"'s, even though it was
        # created after it — the latest message per session is what counts.
        await messages_db.insert(MessageDoc(
            session_id=quiet.id, role="user", content="a while ago",
            created_at=now - timedelta(hours=2),
        ))
        await messages_db.insert(MessageDoc(
            session_id=active.id, role="user", content="just now",
            created_at=now,
        ))
        return await session_service.list_for_user("u1")

    sessions = _run(go)
    assert [s.title for s in sessions] == ["active", "quiet"]


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
