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
    Conflict,
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


# ── stored rows from before alert actions ───────────────────────────

@pytest.mark.parametrize("old, extra, now_reads", [
    ("snoozed", {}, "new"),
    ("delegated", {}, "new"),
    ("completed", {}, "new"),
    ("dismissed", {}, "not_useful"),
    ("acted", {}, "done_by_me"),
    ("acted", {"session_id": "s1"}, "done_by_gaca"),                      # a chat or run was involved
    ("acted", {"resolution_note": "Taken to chat"}, "done_by_gaca"),
    ("new", {}, "new"),
    ("noted", {}, "noted"),
])
def test_old_statuses_read_as_the_new_vocabulary(old, extra, now_reads):
    """Rows are normalised on read; nothing in storage needs rewriting."""
    alert = AlertDoc.model_validate(
        {"user_id": "u1", "kind": "k", "title": "t", "status": old, "snooze_until": "2026-08-09T12:00:00", **extra}
    )
    assert alert.status == now_reads



# ── listing ─────────────────────────────────────────────────────────

def test_list_filters_on_status(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        for title, status in (("open", "new"), ("done", "done_by_gaca"), ("meh", "not_useful")):
            await alerts_db.insert(_with_status(status, title=title))
        open_ = await alert_service.list_for_user("u1", statuses=["new"])
        closed = await alert_service.list_for_user("u1", statuses=["done_by_gaca", "not_useful"])
        return open_, closed

    open_, closed = _run(go)
    assert [a.title for a in open_] == ["open"]
    assert sorted(a.title for a in closed) == ["done", "meh"]



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
        await alert_service.create("u1", _payload(crew="home-alerts", title="mine"))
        await alert_service.create("u1", _payload(crew="work-assistant", title="theirs"))
        await alert_service.create("u1", _payload(title="unset"))
        return await alert_service.list_for_user("u1", crews=["home-alerts"])

    assert [a.title for a in _run(go)] == ["mine"]


# ── triage transitions ──────────────────────────────────────────────

def _with_status(status: str, **fields) -> AlertDoc:
    return AlertDoc(**{"user_id": "u1", "kind": "k", "title": "t", "status": status, **fields})


def test_close_records_how_it_ended_and_the_note(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        out = {}
        for reason in ("done_by_me", "noted", "not_useful"):
            alert, _ = await alert_service.create("u1", _payload(title=reason))
            before = alert.status_changed_at
            closed = await alert_service.close("u1", alert.id, reason, note=f"because {reason}")
            reloaded = await alert_service.get("u1", alert.id)
            out[reason] = (closed, reloaded, before)
        return out

    for reason, (closed, reloaded, before) in _run(go).items():
        assert closed.status == reloaded.status == reason
        assert closed.resolution_note == f"because {reason}"
        assert closed.status_changed_at >= before


def test_done_by_gaca_cannot_be_chosen_by_hand(tmp_path):
    """Only a successful action or take-to-chat says the agent did it."""
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        alert, _ = await alert_service.create("u1", _payload())
        for bad in ("done_by_gaca", "new", "snoozed", "acted"):
            with pytest.raises(InvalidRequest):
                await alert_service.close("u1", alert.id, bad)
        return await alert_service.get("u1", alert.id)

    assert _run(go).status == "new"


def test_the_old_act_and_dismiss_map_onto_close(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        a, _ = await alert_service.create("u1", _payload(title="a"))
        b, _ = await alert_service.create("u1", _payload(title="b"))
        return (await alert_service.act("u1", a.id)).status, (await alert_service.dismiss("u1", b.id)).status

    assert _run(go) == ("done_by_me", "not_useful")


def test_closing_with_no_note_keeps_the_existing_resolution_note(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        await alerts_db.insert(_with_status("new", resolution_note="earlier note"))
        alert = (await alerts_db.find_by_user("u1"))[0]
        return await alert_service.close("u1", alert.id, "noted")

    assert _run(go).resolution_note == "earlier note"


@pytest.mark.parametrize("closed", ["done_by_gaca", "done_by_me", "noted", "not_useful"])
def test_a_closed_alert_rejects_closing_again(tmp_path, closed):
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        await alerts_db.insert(_with_status(closed))
        alert = (await alerts_db.find_by_user("u1"))[0]
        with pytest.raises(InvalidTransition):
            await alert_service.close("u1", alert.id, "noted")

    _run(go)


def test_transitions_are_scoped_to_the_owner(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        theirs, _ = await alert_service.create("u2", _payload())
        with pytest.raises(NotFound):
            await alert_service.close("u1", theirs.id, "noted")
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


def test_running_an_action_opens_the_alert_chat_and_records_a_running_run(tmp_path, monkeypatch):
    settings.db_path = str(tmp_path / "t.db")
    _install_backend(monkeypatch, output="done")

    async def go():
        alert, _ = await alert_service.create(
            "u1", _payload(suggested_actions=[{"label": "Clean logs", "prompt": "Free space"}])
        )
        return await alert_service.run_action(_user(), alert.id, suggested_action_index=0)

    # Asserted on the return value, before the run has had a chance to settle:
    # run_action() is non-blocking, same as start_turn underneath it.
    started = _run(go)
    assert started.status == "new"                      # open while it runs
    assert started.session_id is not None
    [run] = started.runs
    assert (run.state, run.label, run.action_index, run.session_id) == (
        "running", "Clean logs", 0, started.session_id)


def test_a_successful_run_records_the_result_and_closes_the_alert_done_by_gaca(tmp_path, monkeypatch):
    settings.db_path = str(tmp_path / "t.db")
    _install_backend(monkeypatch, output="Added to your calendar.\nhttps://cal.example.com/e/1")

    async def go():
        alert, _ = await alert_service.create("u1", _payload())
        await alert_service.run_action(_user(), alert.id)
        await _drain_background()
        return await alert_service.get("u1", alert.id)

    settled = _run(go)
    assert settled.status == "done_by_gaca"
    [run] = settled.runs
    assert run.state == "done" and run.finished_at is not None
    assert "Added to your calendar" in run.summary
    assert [link.url for link in run.links] == ["https://cal.example.com/e/1"]
    assert settled.resolution_note == run.summary


def test_a_failed_run_leaves_the_alert_open_with_the_error_on_the_run(tmp_path, monkeypatch):
    """A failed action can never silently vanish or close the alert — it stays in the inbox
    with what went wrong, and can be tried again."""
    settings.db_path = str(tmp_path / "t.db")
    _install_backend(monkeypatch, error="hermes gateway timed out")

    async def go():
        alert, _ = await alert_service.create("u1", _payload())
        await alert_service.run_action(_user(), alert.id)
        await _drain_background()
        return await alert_service.get("u1", alert.id)

    failed = _run(go)
    assert failed.status == "new"
    [run] = failed.runs
    assert (run.state, run.error) == ("failed", "hermes gateway timed out")


def test_a_failed_run_can_be_retried_in_the_same_chat(tmp_path, monkeypatch):
    settings.db_path = str(tmp_path / "t.db")
    _install_backend(monkeypatch, error="boom")

    async def go():
        alert, _ = await alert_service.create("u1", _payload())
        await alert_service.run_action(_user(), alert.id)
        await _drain_background()
        _install_backend(monkeypatch, output="worked")
        await alert_service.run_action(_user(), alert.id)
        await _drain_background()
        return await alert_service.get("u1", alert.id)

    done = _run(go)
    assert [r.state for r in done.runs] == ["failed", "done"]
    assert len({r.session_id for r in done.runs}) == 1      # one chat per alert
    assert done.status == "done_by_gaca"


def test_only_one_action_runs_at_a_time(tmp_path, monkeypatch):
    settings.db_path = str(tmp_path / "t.db")
    _install_backend(monkeypatch, output="ok")

    async def go():
        alert, _ = await alert_service.create("u1", _payload())
        await alert_service.run_action(_user(), alert.id)
        with pytest.raises(Conflict):
            await alert_service.run_action(_user(), alert.id)
        await _drain_background()

    _run(go)


def test_a_run_that_finishes_after_the_alert_was_closed_does_not_reopen_it(tmp_path, monkeypatch):
    settings.db_path = str(tmp_path / "t.db")
    _install_backend(monkeypatch, output="done anyway")

    async def go():
        alert, _ = await alert_service.create("u1", _payload())
        await alert_service.run_action(_user(), alert.id)
        await alert_service.close("u1", alert.id, "not_useful")
        await _drain_background()
        return await alert_service.get("u1", alert.id)

    settled = _run(go)
    assert settled.status == "not_useful"                   # the person's choice stands
    assert settled.runs[0].state == "done"                  # but the result is still recorded


def test_a_refired_alert_forgets_earlier_runs_and_chat(tmp_path, monkeypatch):
    settings.db_path = str(tmp_path / "t.db")
    _install_backend(monkeypatch, output="ok")

    async def go():
        first, _ = await alert_service.create("u1", _payload(dedupe_key="k"))
        await alert_service.run_action(_user(), first.id)
        await _drain_background()
        again, created = await alert_service.create("u1", _payload(dedupe_key="k"))
        return created, again

    created, again = _run(go)
    assert created is False
    assert again.status == "new" and again.runs == [] and again.session_id is None


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
            await alert_service.run_action(_user(crew_configured=False), alert.id)
        return await alert_service.get("u1", alert.id)

    unchanged = _run(go)
    assert unchanged.status == "new"
    assert unchanged.session_id is None


@pytest.mark.parametrize("settled", ["done_by_gaca", "done_by_me", "noted", "not_useful"])
def test_running_an_action_on_a_closed_alert_is_rejected(tmp_path, settled):
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        await alerts_db.insert(_with_status(settled))
        alert = (await alerts_db.find_by_user("u1"))[0]
        with pytest.raises(InvalidTransition):
            await alert_service.run_action(_user(), alert.id)

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
        await alert_service.run_action(_user(), alert.id, suggested_action_index=1)
        await _drain_background()

    _run(go)
    assert stub.user_message.endswith("Compress the largest files")
    assert "Disk is 91% full" in stub.user_message       # the agent is also given the alert itself


def test_delegate_combines_action_and_free_text_instruction(tmp_path, monkeypatch):
    settings.db_path = str(tmp_path / "t.db")
    stub = _install_backend(monkeypatch, output="ok")

    async def go():
        alert, _ = await alert_service.create(
            "u1", _payload(suggested_actions=[{"label": "Clean logs", "prompt": "Free space"}])
        )
        await alert_service.run_action(
            _user(),
            alert.id,
            instruction="but don't touch anything under /var/lib/gaca",
            suggested_action_index=0,
        )
        await _drain_background()

    _run(go)
    assert stub.user_message.endswith("Free space\n\nbut don't touch anything under /var/lib/gaca")


def test_delegate_falls_back_to_title_and_body_with_no_action_or_instruction(tmp_path, monkeypatch):
    settings.db_path = str(tmp_path / "t.db")
    stub = _install_backend(monkeypatch, output="ok")

    async def go():
        alert, _ = await alert_service.create(
            "u1", _payload(title="Disk is 91% full", body="mount /")
        )
        await alert_service.run_action(_user(), alert.id)
        await _drain_background()

    _run(go)
    assert "Disk is 91% full\n\nmount /" in stub.user_message
    assert stub.user_message.rstrip().endswith("Disk is 91% full\n\nmount /")


def test_delegate_rejects_an_out_of_range_suggested_action_index(tmp_path):
    """Raised before the crew is ever touched — works even with no crew
    member configured, since it's pure validation against the alert's own
    data."""
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        alert, _ = await alert_service.create("u1", _payload())  # no suggested_actions
        with pytest.raises(InvalidRequest):
            await alert_service.run_action(
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
        await alert_service.run_action(_multi_crew_user(), alert.id)
        await _drain_background()

    _run(go)
    assert calls == [("http://agent.example.com:8644", "trading-key")]


def test_delegate_explicit_crew_overrides_the_alerts_own_crew(tmp_path, monkeypatch):
    settings.db_path = str(tmp_path / "t.db")
    calls = _install_recording_backend(monkeypatch)

    async def go():
        alert, _ = await alert_service.create("u1", _payload(crew="trading"))
        await alert_service.run_action(_multi_crew_user(), alert.id, crew="toolshed")
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
        await alert_service.run_action(
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
        await alert_service.run_action(
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
            await alert_service.run_action(
                _multi_crew_user(), alert.id, crew="nonexistent"
            )

    _run(go)


# ── take to chat (open the alert's chat; send nothing; the agent gets the alert as context) ────
def test_take_to_chat_opens_a_session_with_no_draft_and_sends_nothing(tmp_path, monkeypatch):
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
    assert draft is None                                              # the alert reaches the agent as context, not as typed text
    assert (session.alert_id, session.crew) == (taken.id, "default")  # same link + crew rules as an action run
    assert messages == []                                             # nothing was sent, nothing stored
    assert calls == []                                                # and no agent was even contacted
    assert (taken.status, taken.session_id, taken.resolution_note) == ("done_by_gaca", session.id, "Taken to chat")


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


def test_take_to_chat_closes_an_open_alert_but_leaves_closed_ones(tmp_path, monkeypatch):
    settings.db_path = str(tmp_path / "t.db")
    _install_recording_backend(monkeypatch)

    async def go():
        open_, _ = await alert_service.create("u1", _payload(title="a"))
        meh, _ = await alert_service.create("u1", _payload(title="b"))
        await alert_service.close("u1", meh.id, "not_useful", "not needed")
        return (
            (await alert_service.take_to_chat(_user(), open_.id))[0],
            (await alert_service.take_to_chat(_user(), meh.id))[0],
        )

    from_open, from_closed = _run(go)
    assert from_open.status == "done_by_gaca"
    # Already closed: the chat still opens, but the alert keeps its status and its own note.
    assert (from_closed.status, from_closed.resolution_note) == ("not_useful", "not needed")
    assert from_closed.session_id


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
    assert d1 is None and d2 is None and d3 == "Free space"  # a draft only when you pick an action


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


# ── one-call delegation (POST /alerts/delegations) ───────────────────

def test_delegate_new_raises_and_delegates_in_one_step(tmp_path, monkeypatch):
    """Same rules the Android app applied itself: kind manual.delegation, titled
    with the instruction's first line (max 120), body = the instruction, and
    attributed to whoever handles it — then delegated with that instruction."""
    settings.db_path = str(tmp_path / "t.db")
    calls = _install_recording_backend(monkeypatch)
    instruction = "  " + "x" * 130 + "\r\nthen tell me how it went  "

    async def go():
        alert = await alert_service.delegate_new(
            _multi_crew_user(default_crew="trading"), instruction, crew="toolshed"
        )
        await _drain_background()
        return alert, await messages_db.find_by_session(alert.session_id)

    # The service is handed the schema-trimmed instruction, as the route does.
    from gaca_api.schemas.alert import DelegationCreate

    instruction = DelegationCreate(instruction=instruction).instruction
    alert, msgs = _run(go)
    assert alert.status == "new" and alert.session_id and alert.runs[0].state in ("running", "done")
    assert (alert.kind, alert.title, alert.body) == ("manual.delegation", "x" * 120, instruction)
    assert (alert.crew, alert.source) == ("toolshed", "app")
    # (resolved twice: once up front, so nothing is stored if it can't be reached)
    assert set(calls) == {("http://agent.example.com:8643", "toolshed-key")}
    assert msgs[0].content == instruction  # what the chat records as said


def test_delegate_new_defaults_the_crew_and_stores_nothing_on_a_bad_one(tmp_path, monkeypatch):
    settings.db_path = str(tmp_path / "t.db")
    _install_recording_backend(monkeypatch)

    async def go():
        user = _multi_crew_user(default_crew="trading")
        default = await alert_service.delegate_new(user, "check the logs")
        await _drain_background()
        with pytest.raises(InvalidRequest):
            await alert_service.delegate_new(user, "nope", crew="nonexistent")
        with pytest.raises(CrewNotConfigured):
            await alert_service.delegate_new(_user("u2", crew_configured=False), "nope")
        return default, await alert_service.list_for_user("u1"), await alert_service.list_for_user("u2")

    default, u1_alerts, u2_alerts = _run(go)
    assert default.crew == "trading"
    assert [a.id for a in u1_alerts] == [default.id]  # no orphan alert from the bad call
    assert u2_alerts == []


def test_delegation_instruction_must_not_be_blank():
    from pydantic import ValidationError

    from gaca_api.schemas.alert import DelegationCreate

    with pytest.raises(ValidationError):
        DelegationCreate(instruction="  \n ")
    assert DelegationCreate(instruction=" do it ").instruction == "do it"


# ── AlertOut: data_rows and the resolved artifact ────────────────────

def test_data_rows_render_every_shape_in_order():
    from gaca_api.schemas.alert import AlertOut

    doc = AlertDoc(user_id="u1", kind="k", title="t", data={
        "mount": "/", "used_pct": 91, "free_gb": 12.4, "ok": False, "note": None,
        "disk": {"name": "sda", "tags": ["ssd", 2], "meta": {"x": True}},
        "hosts": ["a", {"b": 1}],
    })
    out = AlertOut(**doc.model_dump(), effective_status="new")
    assert [(r.key, r.value) for r in out.data_rows] == [
        ("mount", "/"), ("used_pct", "91"), ("free_gb", "12.4"), ("ok", "false"), ("note", "null"),
        ("disk", "name: sda, tags: ssd, 2, meta: x: true"),
        ("hosts", "a, b: 1"),
    ]


def test_alerts_carry_their_resolved_artifact(tmp_path):
    from gaca_api.schemas.artifact import ArtifactCreate
    from gaca_api.services import artifacts as artifact_service

    settings.db_path = str(tmp_path / "t.db")

    async def go():
        page = await artifact_service.create(
            "u1", ArtifactCreate(title="Week", html="<p>", description="Calendar", crew="hermes")
        )
        with_page, _ = await alert_service.create("u1", _payload(artifact_id=page.id))
        without, _ = await alert_service.create("u1", _payload())
        # The page is deleted after the alert pointed at it: the chip stays, as missing.
        gone_page = await artifact_service.create("u1", ArtifactCreate(title="Gone", html="<p>"))
        gone, _ = await alert_service.create("u1", _payload(artifact_id=gone_page.id))
        await artifact_service.delete("u1", gone_page.id)
        outs = await alert_service.to_out("u1", [with_page, without, gone])
        return page.id, gone_page.id, outs

    page_id, gone_id, (with_page, without, gone) = _run(go)
    assert with_page.artifact.model_dump() == {
        "id": page_id, "title": "Week", "description": "Calendar", "crew": "hermes", "missing": False
    }
    assert without.artifact is None
    assert gone.artifact.model_dump() == {
        "id": gone_id, "title": "Page", "description": None, "crew": None, "missing": True
    }
