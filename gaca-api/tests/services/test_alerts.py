"""Tests for the alert service: creation, dedupe, effective status, scoping.

The dedupe and effective-status rules are the two pieces of logic here that
aren't obvious from reading the code, so they get the most attention.
"""
import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from gaca_api.config import settings
from gaca_api.db import alerts as alerts_db
from gaca_api.db import client
from gaca_api.models.alert import AlertDoc
from gaca_api.schemas.alert import AlertCreate
from gaca_api.services import alerts as alert_service
from gaca_api.services.exceptions import NotFound

NOW = datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc)


def _run(factory):
    """Open a fresh DB, run the coroutine the factory builds, close it."""

    async def main():
        await client.connect()
        try:
            return await factory()
        finally:
            await client.close()

    return asyncio.run(main())


def _payload(**overrides) -> AlertCreate:
    base = dict(kind="disk.low", title="Disk is 91% full", severity="warn")
    return AlertCreate(**{**base, **overrides})


# ── creation ────────────────────────────────────────────────────────

def test_create_persists_and_is_retrievable(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        alert, created = await alert_service.create("u1", _payload())
        return alert, created, await alert_service.get("u1", alert.id)

    alert, created, fetched = _run(go)
    assert created is True
    assert fetched.id == alert.id
    assert fetched.user_id == "u1"
    assert fetched.status == "new"
    assert fetched.severity == "warn"


def test_kind_is_not_constrained_to_a_known_set(tmp_path):
    """Agents must be able to invent a kind without an API release."""
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        alert, _ = await alert_service.create(
            "u1", _payload(kind="something.nobody.anticipated")
        )
        return alert

    assert _run(go).kind == "something.nobody.anticipated"


# ── dedupe ──────────────────────────────────────────────────────────

def test_dedupe_updates_in_place_instead_of_creating(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        first, created_first = await alert_service.create(
            "u1", _payload(dedupe_key="disk.low:/", title="Disk is 91% full")
        )
        second, created_second = await alert_service.create(
            "u1", _payload(dedupe_key="disk.low:/", title="Disk is 96% full")
        )
        return first, created_first, second, created_second, await alerts_db.find_by_user("u1")

    first, created_first, second, created_second, all_alerts = _run(go)
    assert created_first is True and created_second is False
    # Same alert, updated — not a second one pushed at the user.
    assert second.id == first.id
    assert second.title == "Disk is 96% full"
    assert second.created_at == first.created_at  # identity preserved
    assert len(all_alerts) == 1


def test_dedupe_resurfaces_an_alert_the_user_already_dismissed(tmp_path):
    """A re-fired condition must come back, not stay silently dismissed."""
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        alert, _ = await alert_service.create("u1", _payload(dedupe_key="disk.low:/"))
        # simulate the user having triaged it away
        alert.status = "dismissed"
        alert.resolution_note = "not now"
        await alerts_db.replace(alert)

        again, created = await alert_service.create(
            "u1", _payload(dedupe_key="disk.low:/")
        )
        return again, created

    again, created = _run(go)
    assert created is False
    assert again.status == "new"
    assert again.resolution_note is None


def test_dedupe_is_scoped_per_user(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        a, _ = await alert_service.create("u1", _payload(dedupe_key="disk.low:/"))
        b, _ = await alert_service.create("u2", _payload(dedupe_key="disk.low:/"))
        return a, b

    a, b = _run(go)
    # Two people can care about the same condition without colliding.
    assert a.id != b.id


def test_alerts_without_a_dedupe_key_never_merge(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        await alert_service.create("u1", _payload())
        await alert_service.create("u1", _payload())
        return await alerts_db.find_by_user("u1")

    assert len(_run(go)) == 2


def test_unique_index_is_the_backstop_for_dedupe(tmp_path):
    """The service checks first, but two concurrent posts can race that check."""
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        await alert_service.create("u1", _payload(dedupe_key="disk.low:/"))
        with pytest.raises(sqlite3.IntegrityError):
            await alerts_db.insert(
                AlertDoc(
                    user_id="u1",
                    kind="disk.low",
                    title="racing insert",
                    dedupe_key="disk.low:/",
                )
            )

    _run(go)


# ── effective status ────────────────────────────────────────────────

def test_elapsed_snooze_reads_as_new():
    alert = AlertDoc(
        user_id="u1",
        kind="disk.low",
        title="t",
        status="snoozed",
        snooze_until=NOW - timedelta(seconds=1),
    )
    assert alert.status == "snoozed"  # stored value is untouched
    assert alert_service.effective_status(alert, NOW) == "new"


def test_future_snooze_stays_snoozed():
    alert = AlertDoc(
        user_id="u1",
        kind="disk.low",
        title="t",
        status="snoozed",
        snooze_until=NOW + timedelta(hours=1),
    )
    assert alert_service.effective_status(alert, NOW) == "snoozed"


def test_other_statuses_pass_through_untouched():
    for status in ("new", "delegated", "acted", "dismissed"):
        alert = AlertDoc(user_id="u1", kind="k", title="t", status=status)
        assert alert_service.effective_status(alert, NOW) == status


def test_naive_snooze_until_does_not_explode():
    """Comparing a naive datetime with an aware one raises TypeError.

    The model coerces naive input to UTC precisely so the most-called endpoint
    in the app can't 500 on it.
    """
    alert = AlertDoc(
        user_id="u1",
        kind="k",
        title="t",
        status="snoozed",
        snooze_until=datetime(2026, 8, 9, 11, 0, 0),  # no tzinfo
    )
    assert alert_service.effective_status(alert, NOW) == "new"


# ── listing ─────────────────────────────────────────────────────────

def test_list_filters_on_effective_status_not_stored_status(tmp_path):
    """A deferred alert has to come back on its own, or snooze is a black hole."""
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        elapsed, _ = await alert_service.create("u1", _payload(title="elapsed"))
        elapsed.status = "snoozed"
        elapsed.snooze_until = NOW - timedelta(minutes=1)
        await alerts_db.replace(elapsed)

        pending, _ = await alert_service.create("u1", _payload(title="still snoozed"))
        pending.status = "snoozed"
        pending.snooze_until = NOW + timedelta(hours=2)
        await alerts_db.replace(pending)

        as_new = await alert_service.list_for_user("u1", statuses=["new"], now=NOW)
        as_snoozed = await alert_service.list_for_user(
            "u1", statuses=["snoozed"], now=NOW
        )
        return as_new, as_snoozed

    as_new, as_snoozed = _run(go)
    assert [a.title for a in as_new] == ["elapsed"]
    assert [a.title for a in as_snoozed] == ["still snoozed"]


def test_list_is_scoped_to_the_owner(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        await alert_service.create("u1", _payload(title="mine"))
        await alert_service.create("u2", _payload(title="theirs"))
        return await alert_service.list_for_user("u1")

    assert [a.title for a in _run(go)] == ["mine"]


def test_get_someone_elses_alert_raises_not_found(tmp_path):
    """404, not 403 — a 403 would confirm the id exists."""
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        theirs, _ = await alert_service.create("u2", _payload())
        with pytest.raises(NotFound):
            await alert_service.get("u1", theirs.id)

    _run(go)


def test_list_filters_by_severity(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        await alert_service.create("u1", _payload(severity="info", title="quiet"))
        await alert_service.create("u1", _payload(severity="urgent", title="loud"))
        return await alert_service.list_for_user("u1", severities=["urgent"])

    assert [a.title for a in _run(go)] == ["loud"]


def test_list_is_newest_first_and_respects_limit(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        # Explicit timestamps: alerts created in the same millisecond would
        # otherwise order arbitrarily.
        for i in range(4):
            await alerts_db.insert(
                AlertDoc(
                    user_id="u1",
                    kind="k",
                    title=f"alert-{i}",
                    created_at=NOW + timedelta(minutes=i),
                )
            )
        return await alert_service.list_for_user("u1", limit=2)

    assert [a.title for a in _run(go)] == ["alert-3", "alert-2"]
