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
    assert [s.title for s, _ in sessions] == ["second", "first"]


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
    assert [s.title for s, _ in sessions] == ["old but active", "new but quiet"]


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
    assert [s.title for s, _ in sessions] == ["active", "quiet"]


def test_get_with_messages_is_scoped_to_the_owner(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        created = await session_service.create("u1", None, "hermes")
        with pytest.raises(NotFound):
            await session_service.get_with_messages("u2", created.id)
        session, messages, _ = await session_service.get_with_messages("u1", created.id)
        return session, messages

    session, messages = _run(go)
    assert session.user_id == "u1"
    assert messages == []


def _msg(session_id, role, content, minutes=0):
    return MessageDoc(
        session_id=session_id, role=role, content=content,
        created_at=datetime.now(timezone.utc) + timedelta(minutes=minutes),
    )


def test_untitled_session_is_labeled_from_its_opening_and_latest_messages(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        s = await session_service.create("u1", None, "hermes")
        await messages_db.insert(_msg(s.id, "user", "Can you **move** the dentist\nappointment to Friday?", 0))
        await messages_db.insert(_msg(s.id, "assistant", "Done - it's now Friday at 9.", 1))
        await session_service.create("u1", None, "hermes")  # no messages yet
        return await session_service.list_for_user("u1")

    rows = {s.id: labels for s, labels in _run(go)}
    labeled = next(l for l in rows.values() if l.summary)
    assert labeled.summary == "Can you move the dentist appointment to Friday?"
    assert labeled.preview == "Done - it's now Friday at 9."
    empty = next(l for l in rows.values() if not l.summary)
    assert empty.preview is None


def test_explicit_title_wins_and_user_preview_is_prefixed(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        s = await session_service.create("u1", "Trip planning", "hermes")
        await messages_db.insert(_msg(s.id, "user", "something else entirely", 0))
        return await session_service.list_for_user("u1")

    [(_, labels)] = _run(go)
    assert labels.summary == "Trip planning"
    assert labels.preview == "You: something else entirely"


def test_long_text_is_cut_at_a_word_boundary():
    from gaca_api.services.sessions import SUMMARY_MAX, labels_for

    text = "word " * 40
    labels = labels_for(None, _msg("s", "user", text), None)
    assert labels.summary.endswith("…") and len(labels.summary) <= SUMMARY_MAX + 1
    assert not labels.summary[:-1].endswith("wor")


def test_get_with_messages_returns_labels(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        s = await session_service.create("u1", None, "hermes")
        await messages_db.insert(_msg(s.id, "user", "hello there", 0))
        await messages_db.insert(_msg(s.id, "assistant", "hi!", 1))
        return await session_service.get_with_messages("u1", s.id)

    _, _, labels = _run(go)
    assert (labels.summary, labels.preview) == ("hello there", "hi!")


# --- rendered transcript: display_content + artifact chips ------------------

def test_messages_resolve_artifact_links_for_the_owner_only(tmp_path):
    """Owned artifacts resolve to their real title/description/crew; a link to
    nothing — or to someone ELSE's artifact — still shows, as missing, titled
    from the link text (or "Page"), without leaking the other user's title."""
    from gaca_api.schemas.artifact import ArtifactCreate
    from gaca_api.services import artifacts as artifact_service

    settings.db_path = str(tmp_path / "t.db")

    async def scenario():
        s = await session_service.create("u1", None, "hermes")
        mine = await artifact_service.create(
            "u1", ArtifactCreate(title="Groceries", html="<p>", description="Weekly list", crew="hermes")
        )
        theirs = await artifact_service.create("u2", ArtifactCreate(title="Secret", html="<p>"))
        gone = "f" * 32
        await messages_db.insert(MessageDoc(
            session_id=s.id, role="assistant",
            content=(
                f"Made it: [My list](gaca://artifact/{mine.id})\n\n"
                f"Also [Theirs](gaca://artifact/{theirs.id}) gaca://artifact/{gone} "
                f"gaca://artifact/{mine.id}"
            ),
        ))
        return mine.id, theirs.id, gone, await session_service.get_detail("u1", s.id)

    mine_id, theirs_id, gone, detail = _run(scenario)
    message = detail.messages[0]
    assert message.display_content == "Made it:\n\nAlso"
    assert [a.model_dump() for a in message.artifacts] == [
        {"id": mine_id, "title": "Groceries", "description": "Weekly list", "crew": "hermes", "missing": False},
        {"id": theirs_id, "title": "Theirs", "description": None, "crew": None, "missing": True},
        {"id": gone, "title": "Page", "description": None, "crew": None, "missing": True},
    ]
    assert message.activity is None  # no run activity stored on this message


def test_labels_do_not_show_raw_artifact_links(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def scenario():
        s = await session_service.create("u1", None, "hermes")
        await messages_db.insert(MessageDoc(
            session_id=s.id, role="assistant", content=f"Here you go [List](gaca://artifact/{'a' * 32})"
        ))
        return await session_service.get_detail("u1", s.id)

    assert _run(scenario).preview == "Here you go"


def test_last_activity_is_the_latest_message_else_creation(tmp_path):
    settings.db_path = str(tmp_path / "t.db")
    later = datetime(2030, 1, 1, tzinfo=timezone.utc)

    async def scenario():
        quiet = await session_service.create("u1", None, "hermes")
        busy = await session_service.create("u1", None, "hermes")
        await messages_db.insert(MessageDoc(session_id=busy.id, role="user", content="hi", created_at=later))
        return (
            quiet,
            await session_service.get_detail("u1", quiet.id),
            await session_service.get_detail("u1", busy.id),
            await session_service.list_for_user("u1"),
        )

    quiet, quiet_detail, busy_detail, listed = _run(scenario)
    assert [labels.last_activity_at for _, labels in listed] == [later, None]  # None -> created_at
    assert quiet_detail.last_activity_at == quiet.created_at
    assert busy_detail.last_activity_at == later
