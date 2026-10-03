"""Tests for the alert service: creation, dedupe, effective status, scoping.

The dedupe and effective-status rules are the two pieces of logic here that
aren't obvious from reading the code, so they get the most attention.
"""
import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import gaca_api.agent.runtime as agent_runtime
import gaca_api.services.runs as runs
from gaca_api.config import settings
from gaca_api.db import alerts as alerts_db
from gaca_api.db import client
from gaca_api.db import messages as messages_db
from gaca_api.models.alert import AlertDoc
from gaca_api.models.user import CrewMember, UserDoc
from gaca_api.schemas.alert import AlertCreate
from gaca_api.schemas.event import RunCompleted, RunFailed
from gaca_api.services import alerts as alert_service
from gaca_api.services import sessions as session_service
from gaca_api.services.exceptions import (
    CrewNotConfigured,
    InvalidRequest,
    InvalidTransition,
    NotFound,
)

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


def test_dedupe_replaces_content_rather_than_merging_it(tmp_path):
    """Omitted fields revert to defaults. Intentional — see services.alerts.create.

    Pinned because it reads like a bug: without this test someone "fixes" it to
    a merge, and stale suggested_actions start surviving re-posts. Their prompts
    get handed to an agent, so wrong ones are worse than missing ones.
    """
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        await alert_service.create(
            "u1",
            _payload(
                dedupe_key="disk.low:/",
                source="hermes",
                body="detail from the first post",
                data={"used_pct": 91},
                suggested_actions=[{"label": "Clean logs", "prompt": "Free space…"}],
            ),
        )
        # A terse re-post: only the required fields plus the key.
        terse, _ = await alert_service.create(
            "u1", AlertCreate(kind="disk.low", title="Disk is 96% full",
                              dedupe_key="disk.low:/")
        )
        return terse

    terse = _run(go)
    assert terse.suggested_actions == []
    assert terse.body is None
    assert terse.data == {}
    assert terse.source == "unknown"
    assert terse.severity == "info"


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


def test_list_filters_by_crew(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        await alert_service.create("u1", _payload(crew="david-alerts", title="mine"))
        await alert_service.create("u1", _payload(crew="work-assistant", title="theirs"))
        await alert_service.create("u1", _payload(title="unset"))
        return await alert_service.list_for_user("u1", crews=["david-alerts"])

    assert [a.title for a in _run(go)] == ["mine"]


# ── triage transitions ──────────────────────────────────────────────

def _with_status(status: str, **fields) -> AlertDoc:
    return AlertDoc(user_id="u1", kind="k", title="t", status=status, **fields)


def test_act_settles_the_alert_and_records_the_note(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        alert, _ = await alert_service.create("u1", _payload())
        before = alert.status_changed_at
        acted = await alert_service.act("u1", alert.id, note="did it by hand")
        return acted, before, await alert_service.get("u1", alert.id)

    acted, before, reloaded = _run(go)
    assert acted.status == "acted"
    assert acted.resolution_note == "did it by hand"
    assert acted.status_changed_at >= before
    assert reloaded.status == "acted"  # persisted, not just returned


def test_dismiss_settles_the_alert(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        alert, _ = await alert_service.create("u1", _payload())
        return await alert_service.dismiss("u1", alert.id, note="not interesting")

    dismissed = _run(go)
    assert dismissed.status == "dismissed"
    assert dismissed.resolution_note == "not interesting"


def test_act_with_no_note_preserves_the_existing_resolution_note(tmp_path):
    """Acting on a `completed` alert with no note of your own keeps Hermes's
    own output rather than silently wiping it to None."""
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        await alerts_db.insert(_with_status("completed", resolution_note="Hermes: cleaned up 4GB"))
        alert = (await alerts_db.find_by_user("u1"))[0]
        return await alert_service.act("u1", alert.id)

    acted = _run(go)
    assert acted.status == "acted"
    assert acted.resolution_note == "Hermes: cleaned up 4GB"


def test_act_with_a_note_overwrites_the_existing_resolution_note(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        await alerts_db.insert(_with_status("completed", resolution_note="Hermes: cleaned up 4GB"))
        alert = (await alerts_db.find_by_user("u1"))[0]
        return await alert_service.act("u1", alert.id, note="looks good, thanks")

    acted = _run(go)
    assert acted.resolution_note == "looks good, thanks"


def test_dismiss_with_no_note_preserves_the_existing_resolution_note(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        await alerts_db.insert(_with_status("completed", resolution_note="Hermes: nothing to report"))
        alert = (await alerts_db.find_by_user("u1"))[0]
        return await alert_service.dismiss("u1", alert.id)

    dismissed = _run(go)
    assert dismissed.resolution_note == "Hermes: nothing to report"


def test_snooze_defers_and_then_returns_on_its_own(tmp_path):
    settings.db_path = str(tmp_path / "t.db")
    until = NOW + timedelta(hours=2)

    async def go():
        alert, _ = await alert_service.create("u1", _payload())
        snoozed = await alert_service.snooze("u1", alert.id, until)
        return snoozed

    snoozed = _run(go)
    assert snoozed.status == "snoozed"
    assert snoozed.snooze_until == until
    # Still deferred an hour before it's due...
    assert alert_service.effective_status(snoozed, until - timedelta(hours=1)) == "snoozed"
    # ...and back in the inbox a second after, with nothing scheduled to do it.
    assert alert_service.effective_status(snoozed, until + timedelta(seconds=1)) == "new"


def test_act_clears_a_pending_snooze(tmp_path):
    """No stale deferral left hanging on a settled alert."""
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        alert, _ = await alert_service.create("u1", _payload())
        await alert_service.snooze("u1", alert.id, NOW + timedelta(days=1))
        return await alert_service.act("u1", alert.id)

    acted = _run(go)
    assert acted.status == "acted"
    assert acted.snooze_until is None


def test_a_snoozed_alert_can_still_be_triaged(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        alert, _ = await alert_service.create("u1", _payload())
        await alert_service.snooze("u1", alert.id, NOW + timedelta(days=1))
        # re-snooze, then settle it
        await alert_service.snooze("u1", alert.id, NOW + timedelta(days=2))
        return await alert_service.dismiss("u1", alert.id)

    assert _run(go).status == "dismissed"


def test_completed_alerts_are_triageable(tmp_path):
    """`completed` behaves exactly like `new` for manual triage — Hermes
    finishing isn't the same as the alert being closed out."""
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        await alerts_db.insert(_with_status("completed"))
        alert = (await alerts_db.find_by_user("u1"))[0]
        return await alert_service.act("u1", alert.id)

    assert _run(go).status == "acted"


def test_completed_alert_can_be_snoozed(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        await alerts_db.insert(_with_status("completed"))
        alert = (await alerts_db.find_by_user("u1"))[0]
        return await alert_service.snooze("u1", alert.id, NOW + timedelta(days=1))

    assert _run(go).status == "snoozed"


def test_completed_alert_can_be_redelegated(tmp_path, monkeypatch):
    settings.db_path = str(tmp_path / "t.db")
    _install_backend(monkeypatch, output="ok")

    async def go():
        await alerts_db.insert(_with_status("completed"))
        alert = (await alerts_db.find_by_user("u1"))[0]
        return await alert_service.delegate(_user(), alert.id)

    assert _run(go).status == "delegated"


@pytest.mark.parametrize("settled", ["acted", "dismissed"])
def test_terminal_states_reject_further_triage(tmp_path, settled):
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        await alerts_db.insert(_with_status(settled))
        alert = (await alerts_db.find_by_user("u1"))[0]
        for move in (alert_service.act, alert_service.dismiss):
            with pytest.raises(InvalidTransition):
                await move("u1", alert.id)
        with pytest.raises(InvalidTransition):
            await alert_service.snooze("u1", alert.id, NOW + timedelta(days=1))

    _run(go)


def test_delegated_alerts_are_settled_by_the_run_not_by_hand(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        await alerts_db.insert(_with_status("delegated"))
        alert = (await alerts_db.find_by_user("u1"))[0]
        with pytest.raises(InvalidTransition):
            await alert_service.act("u1", alert.id)

    _run(go)


def test_transitions_are_scoped_to_the_owner(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        theirs, _ = await alert_service.create("u2", _payload())
        with pytest.raises(NotFound):
            await alert_service.act("u1", theirs.id)
        # and it really is untouched
        return await alert_service.get("u2", theirs.id)

    assert _run(go).status == "new"


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


# ── delegation ──────────────────────────────────────────────────────

def _user(user_id: str = "u1", *, crew_configured: bool = True, **overrides) -> UserDoc:
    """A UserDoc for delegate() tests. Crew-configured by default — the
    handful of tests that care about the unconfigured case pass
    crew_configured=False explicitly."""
    creds = (
        {
            "agent_host": "http://agent.example.com",
            "crew": [CrewMember(name="default", port=8642, api_key="test-key")],
            "default_crew": "default",
        }
        if crew_configured
        else {}
    )
    return UserDoc(
        id=user_id, email=f"{user_id}@example.com", display_name=user_id,
        **creds, **overrides,
    )


class _StubBackend:
    """Agent backend stand-in: yields one canned terminal event per run.

    No Hermes contact — mirrors the StubBackend in tests/services/test_runs.py,
    just parameterized for the two outcomes delegation cares about.
    """

    def __init__(self, *, output: str | None = None, error: str | None = None):
        assert (output is None) != (error is None), "give exactly one outcome"
        self._output = output
        self._error = error
        self.user_message: str | None = None

    async def start_run(self, *, session_id, history, user_message, images=()):
        self.user_message = user_message
        return "run_stub"

    async def stream_events(self, run_id, *, session_id):
        if self._output is not None:
            yield RunCompleted(session_id=session_id, run_id=run_id, output=self._output)
        else:
            yield RunFailed(session_id=session_id, run_id=run_id, error=self._error)


def _install_backend(monkeypatch, **outcome) -> _StubBackend:
    """Stand in for agent_runtime.backend_for — returns the stub regardless of
    which credentials it's asked to resolve, since these tests exercise
    alerts.delegate's orchestration, not runtime's adapter-caching logic."""
    stub = _StubBackend(**outcome)
    monkeypatch.setattr(agent_runtime, "backend_for", lambda base_url, api_key: stub)
    return stub


async def _drain_background() -> None:
    """Let the reader task start_turn spawned finish before asserting settlement."""
    await asyncio.gather(*list(runs._background))


def test_delegate_starts_a_session_and_marks_the_alert_delegated(tmp_path, monkeypatch):
    settings.db_path = str(tmp_path / "t.db")
    _install_backend(monkeypatch, output="done")

    async def go():
        alert, _ = await alert_service.create("u1", _payload())
        return await alert_service.delegate(_user(), alert.id)

    # Asserted on the return value, before the run has had a chance to settle
    # anything — delegate() is non-blocking, same as start_turn underneath it.
    delegated = _run(go)
    assert delegated.status == "delegated"
    assert delegated.session_id is not None
    assert delegated.snooze_until is None


def test_a_completed_run_settles_the_alert_to_completed_with_the_output(tmp_path, monkeypatch):
    """Not `acted` — Hermes finishing isn't the same as the alert being closed
    out. `resolution_note` carries the run's own output so there's something
    to actually look at."""
    settings.db_path = str(tmp_path / "t.db")
    _install_backend(monkeypatch, output="cleaned up 4GB")

    async def go():
        alert, _ = await alert_service.create("u1", _payload())
        await alert_service.delegate(_user(), alert.id)
        await _drain_background()
        return await alert_service.get("u1", alert.id)

    settled = _run(go)
    assert settled.status == "completed"
    assert settled.resolution_note == "cleaned up 4GB"


def test_a_failed_run_bounces_the_alert_back_to_new_with_the_error(tmp_path, monkeypatch):
    """A failed delegation can never silently vanish — same rule as slice 4,
    just triggered by the agent instead of a hand."""
    settings.db_path = str(tmp_path / "t.db")
    _install_backend(monkeypatch, error="hermes gateway timed out")

    async def go():
        alert, _ = await alert_service.create("u1", _payload())
        await alert_service.delegate(_user(), alert.id)
        await _drain_background()
        return await alert_service.get("u1", alert.id)

    bounced = _run(go)
    assert bounced.status == "new"
    assert bounced.resolution_note == "hermes gateway timed out"


def test_delegate_raises_crew_not_configured_before_touching_anything(tmp_path):
    """No crew member submitted yet — the run never starts, and the alert
    stays put."""
    settings.db_path = str(tmp_path / "t.db")
    # Real connect(), not a monkeypatch: this test exercises the actual
    # "runtime initialized, but THIS user has no credentials" state, as
    # opposed to the "lifespan never ran" state every other test sidesteps.
    settings.agent_engine = "hermes"
    agent_runtime.connect()

    async def go():
        alert, _ = await alert_service.create("u1", _payload())
        with pytest.raises(CrewNotConfigured):
            await alert_service.delegate(_user(crew_configured=False), alert.id)
        return await alert_service.get("u1", alert.id)

    unchanged = _run(go)
    assert unchanged.status == "new"
    assert unchanged.session_id is None


@pytest.mark.parametrize("settled", ["acted", "dismissed"])
def test_delegating_a_terminal_alert_is_rejected(tmp_path, settled):
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        await alerts_db.insert(_with_status(settled))
        alert = (await alerts_db.find_by_user("u1"))[0]
        with pytest.raises(InvalidTransition):
            await alert_service.delegate(_user(), alert.id)

    _run(go)


def test_delegate_prefers_the_chosen_suggested_action_prompt(tmp_path, monkeypatch):
    settings.db_path = str(tmp_path / "t.db")
    stub = _install_backend(monkeypatch, output="ok")

    async def go():
        alert, _ = await alert_service.create(
            "u1",
            _payload(
                suggested_actions=[
                    {"label": "Clean logs", "prompt": "Free space by deleting old logs"},
                    {"label": "Compress", "prompt": "Compress the largest files"},
                ]
            ),
        )
        await alert_service.delegate(_user(), alert.id, suggested_action_index=1)
        await _drain_background()

    _run(go)
    assert stub.user_message == "Compress the largest files"


def test_delegate_combines_action_and_free_text_instruction(tmp_path, monkeypatch):
    settings.db_path = str(tmp_path / "t.db")
    stub = _install_backend(monkeypatch, output="ok")

    async def go():
        alert, _ = await alert_service.create(
            "u1", _payload(suggested_actions=[{"label": "Clean logs", "prompt": "Free space"}])
        )
        await alert_service.delegate(
            _user(),
            alert.id,
            instruction="but don't touch anything under /var/lib/gaca",
            suggested_action_index=0,
        )
        await _drain_background()

    _run(go)
    assert stub.user_message == "Free space\n\nbut don't touch anything under /var/lib/gaca"


def test_delegate_falls_back_to_title_and_body_with_no_action_or_instruction(tmp_path, monkeypatch):
    settings.db_path = str(tmp_path / "t.db")
    stub = _install_backend(monkeypatch, output="ok")

    async def go():
        alert, _ = await alert_service.create(
            "u1", _payload(title="Disk is 91% full", body="mount /")
        )
        await alert_service.delegate(_user(), alert.id)
        await _drain_background()

    _run(go)
    assert stub.user_message == "Disk is 91% full\n\nmount /"


def test_delegate_rejects_an_out_of_range_suggested_action_index(tmp_path):
    """Raised before the crew is ever touched — works even with no crew
    member configured, since it's pure validation against the alert's own
    data."""
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        alert, _ = await alert_service.create("u1", _payload())  # no suggested_actions
        with pytest.raises(InvalidRequest):
            await alert_service.delegate(
                _user(crew_configured=False), alert.id, suggested_action_index=0
            )

    _run(go)


# ── delegate: which crew member handles it ───────────────────────────

def _multi_crew_user(user_id: str = "u1", *, default_crew: str = "toolshed") -> UserDoc:
    return UserDoc(
        id=user_id,
        email=f"{user_id}@example.com",
        display_name=user_id,
        agent_host="http://agent.example.com",
        crew=[
            CrewMember(name="toolshed", port=8643, api_key="toolshed-key"),
            CrewMember(name="trading", port=8644, api_key="trading-key"),
        ],
        default_crew=default_crew,
    )


def _install_recording_backend(monkeypatch) -> list[tuple[str, str]]:
    """Like _install_backend, but records every (base_url, api_key) it was
    resolved with, so a test can prove WHICH crew member actually got
    picked."""
    calls: list[tuple[str, str]] = []
    stub = _StubBackend(output="ok")

    def fake_backend_for(base_url: str, api_key: str):
        calls.append((base_url, api_key))
        return stub

    monkeypatch.setattr(agent_runtime, "backend_for", fake_backend_for)
    return calls


def test_delegate_uses_the_alerts_own_crew_by_default(tmp_path, monkeypatch):
    settings.db_path = str(tmp_path / "t.db")
    calls = _install_recording_backend(monkeypatch)

    async def go():
        alert, _ = await alert_service.create("u1", _payload(crew="trading"))
        await alert_service.delegate(_multi_crew_user(), alert.id)
        await _drain_background()

    _run(go)
    assert calls == [("http://agent.example.com:8644", "trading-key")]


def test_delegate_explicit_crew_overrides_the_alerts_own_crew(tmp_path, monkeypatch):
    settings.db_path = str(tmp_path / "t.db")
    calls = _install_recording_backend(monkeypatch)

    async def go():
        alert, _ = await alert_service.create("u1", _payload(crew="trading"))
        await alert_service.delegate(_multi_crew_user(), alert.id, crew="toolshed")
        await _drain_background()

    _run(go)
    assert calls == [("http://agent.example.com:8643", "toolshed-key")]


def test_delegate_falls_back_to_the_default_when_the_alerts_crew_is_unset(
    tmp_path, monkeypatch
):
    settings.db_path = str(tmp_path / "t.db")
    calls = _install_recording_backend(monkeypatch)

    async def go():
        alert, _ = await alert_service.create("u1", _payload())  # no crew
        await alert_service.delegate(
            _multi_crew_user(default_crew="trading"), alert.id
        )
        await _drain_background()

    _run(go)
    assert calls == [("http://agent.example.com:8644", "trading-key")]


def test_delegate_falls_back_to_the_default_when_the_alerts_crew_is_unmatched(
    tmp_path, monkeypatch
):
    """The alert's `crew` is advisory metadata, not a guarantee — a name
    that doesn't match any configured crew member (stale config, typo) falls
    through to the default rather than erroring."""
    settings.db_path = str(tmp_path / "t.db")
    calls = _install_recording_backend(monkeypatch)

    async def go():
        alert, _ = await alert_service.create("u1", _payload(crew="some-other-profile"))
        await alert_service.delegate(
            _multi_crew_user(default_crew="trading"), alert.id
        )
        await _drain_background()

    _run(go)
    assert calls == [("http://agent.example.com:8644", "trading-key")]


def test_delegate_rejects_an_unknown_explicit_crew(tmp_path):
    """Unlike the alert's own `crew`, an explicit override is a deliberate
    choice — naming one that doesn't exist is a real error, not a soft miss."""
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        alert, _ = await alert_service.create("u1", _payload())
        with pytest.raises(InvalidRequest):
            await alert_service.delegate(
                _multi_crew_user(), alert.id, crew="nonexistent"
            )

    _run(go)


# ── take to chat (open the chat with a draft; send nothing; mark handled) ────
def test_take_to_chat_opens_a_session_with_a_draft_and_sends_nothing(tmp_path, monkeypatch):
    settings.db_path = str(tmp_path / "t.db")
    calls = _install_recording_backend(monkeypatch)

    async def go():
        alert, _ = await alert_service.create("u1", _payload(body="Only 9 GB free on /var"))
        taken, session_id, draft = await alert_service.take_to_chat(_user(), alert.id)
        session = await session_service.ensure_owned("u1", session_id)
        messages = await messages_db.find_by_session(session_id)
        await _drain_background()
        return taken, session, messages, draft

    taken, session, messages, draft = _run(go)
    assert draft == "Disk is 91% full\n\nOnly 9 GB free on /var"
    assert (session.alert_id, session.crew) == (taken.id, "default")  # same link + crew rules as delegate
    assert messages == []                                             # nothing was sent, nothing stored
    assert calls == []                                                # and no agent was even contacted
    assert (taken.status, taken.session_id, taken.resolution_note) == ("acted", session.id, "Taken to chat")
    assert taken.snooze_until is None


def test_take_to_chat_with_a_suggested_action_drafts_that_actions_prompt(tmp_path, monkeypatch):
    settings.db_path = str(tmp_path / "t.db")
    _install_recording_backend(monkeypatch)

    async def go():
        alert, _ = await alert_service.create(
            "u1", _payload(suggested_actions=[{"label": "Clean logs", "prompt": "Free space in /var/log"}])
        )
        _, _, draft = await alert_service.take_to_chat(_user(), alert.id, suggested_action_index=0)
        with pytest.raises(InvalidRequest):
            await alert_service.take_to_chat(_user(), alert.id, suggested_action_index=3)
        return draft

    assert _run(go) == "Free space in /var/log"


def test_take_to_chat_handles_snoozed_and_completed_alerts_but_leaves_closed_ones(tmp_path, monkeypatch):
    settings.db_path = str(tmp_path / "t.db")
    _install_recording_backend(monkeypatch)

    async def go():
        snoozed, _ = await alert_service.create("u1", _payload(title="a"))
        await alert_service.snooze("u1", snoozed.id, datetime.now(timezone.utc) + timedelta(hours=2))
        dismissed, _ = await alert_service.create("u1", _payload(title="b"))
        await alert_service.dismiss("u1", dismissed.id, "not needed")
        return (
            (await alert_service.take_to_chat(_user(), snoozed.id))[0],
            (await alert_service.take_to_chat(_user(), dismissed.id))[0],
        )

    from_snoozed, from_dismissed = _run(go)
    assert from_snoozed.status == "acted" and from_snoozed.snooze_until is None
    # Already closed: the chat still opens, but the alert keeps its status and its own note.
    assert (from_dismissed.status, from_dismissed.resolution_note) == ("dismissed", "not needed")
    assert from_dismissed.session_id


def test_take_to_chat_twice_reuses_the_conversation(tmp_path, monkeypatch):
    settings.db_path = str(tmp_path / "t.db")
    _install_recording_backend(monkeypatch)

    async def go():
        alert, _ = await alert_service.create(
            "u1", _payload(suggested_actions=[{"label": "Clean logs", "prompt": "Free space"}])
        )
        _, first, first_draft = await alert_service.take_to_chat(_user(), alert.id)
        _, second, second_draft = await alert_service.take_to_chat(_user(), alert.id)
        _, third, third_draft = await alert_service.take_to_chat(_user(), alert.id, suggested_action_index=0)
        return first, second, third, first_draft, second_draft, third_draft

    first, second, third, d1, d2, d3 = _run(go)
    assert first == second == third  # one conversation, not three
    assert d1 and d2 is None and d3 == "Free space"  # reopening has no draft unless you pick a suggestion


def test_take_to_chat_needs_a_crew_and_is_scoped_to_the_owner(tmp_path, monkeypatch):
    settings.db_path = str(tmp_path / "t.db")
    _install_recording_backend(monkeypatch)

    async def go():
        alert, _ = await alert_service.create("u1", _payload())
        with pytest.raises(NotFound):
            await alert_service.take_to_chat(_user("u2"), alert.id)
        with pytest.raises(CrewNotConfigured):
            await alert_service.take_to_chat(_user(crew_configured=False), alert.id)
        # a failed attempt must not have half-handled the alert
        return await alert_service.get("u1", alert.id)

    still = _run(go)
    assert still.status == "new" and still.session_id is None


# ── an alert carries who posted it, and its chat carries that over ──────────────────────────
def test_the_poster_helpers_agent_field_counts_as_the_crew():
    """The Hermes alerts helper has always sent the posting profile as `agent`; the model used
    to ignore it, so skill-posted alerts had no crew."""
    assert AlertCreate(kind="k", title="t", agent="trading").crew == "trading"
    assert AlertCreate(kind="k", title="t", crew="toolshed").crew == "toolshed"
    assert AlertCreate(kind="k", title="t", crew="toolshed", agent="trading").crew == "toolshed"  # crew wins
    assert AlertCreate(kind="k", title="t").crew is None
    assert AlertCreate(kind="k", title="t", agent="  ").crew is None  # blank means nobody


def test_an_alert_posted_by_a_profile_opens_that_profiles_chat(tmp_path, monkeypatch):
    settings.db_path = str(tmp_path / "t.db")
    _install_recording_backend(monkeypatch)

    async def go():
        alert, _ = await alert_service.create("u1", AlertCreate(kind="k", title="from trading", agent="trading"))
        elsewhere, _ = await alert_service.create("u1", AlertCreate(kind="k", title="no crew"))
        stale, _ = await alert_service.create("u1", AlertCreate(kind="k", title="gone", agent="retired-profile"))
        user = _multi_crew_user(default_crew="toolshed")
        out = {}
        for label, a, explicit in (("own", alert, None), ("override", alert, "toolshed"), ("none", elsewhere, None), ("stale", stale, None)):
            _, session_id, _ = await alert_service.take_to_chat(user, a.id, crew=explicit)
            out[label] = (await session_service.ensure_owned("u1", session_id)).crew
        return alert, out

    alert, crews = _run(go)
    assert alert.crew == "trading"
    assert crews == {
        "own": "trading",         # the poster's profile, with nobody choosing anything
        "override": "trading",    # (the alert already has its chat; asking again reuses it)
        "none": "toolshed",       # no crew on the alert -> the account default
        "stale": "toolshed",      # a crew name that no longer exists -> falls back, never errors
    }


def test_an_explicit_crew_choice_beats_the_alerts_own_crew(tmp_path, monkeypatch):
    settings.db_path = str(tmp_path / "t.db")
    _install_recording_backend(monkeypatch)

    async def go():
        alert, _ = await alert_service.create("u1", AlertCreate(kind="k", title="x", agent="trading"))
        _, session_id, _ = await alert_service.take_to_chat(_multi_crew_user(), alert.id, crew="toolshed")
        return (await session_service.ensure_owned("u1", session_id)).crew

    assert _run(go) == "toolshed"
