"""Reminders (docs/reminders.md): a nudge with a time fires as an alert; a note with none just waits.

Run against the real app and auth. Times are made relative to now: a reminder due a few minutes ago is
"due now" (the API allows an hour of past for an agent that is slightly behind), one due in hours is not."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from gaca_api.services import push as push_service
from tests.test_api_tokens_http import bearer, http, mint  # noqa: F401  (fixture + helpers)

URL = "/api/reminders"


def at(**delta):
    return (datetime.now(timezone.utc) + timedelta(**delta)).replace(microsecond=0).isoformat()


@pytest.fixture(autouse=True)
def pushes(monkeypatch):
    sent = []

    async def fake(user_id, *, title, body, alert_id, severity):
        sent.append({"title": title, "body": body, "alert_id": alert_id, "severity": severity})

    monkeypatch.setattr(push_service, "notify", fake)
    return sent


def make(http, text="Call the dentist", **body):
    r = http.post(URL, headers=http.session, json={"text": text, **body})
    assert r.status_code in (200, 201), r.text
    return r.json()


def fire(http, headers=None):
    r = http.post(f"{URL}/fire-due", headers=headers or http.session)
    assert r.status_code == 200, r.text
    return r.json()


def alerts(http):
    return http.get("/api/alerts?status=new&status=done_by_gaca", headers=http.session).json()["alerts"]


# ── making them ──────────────────────────────────────────────────────

def test_a_timed_reminder_is_created_with_reschedule_presets(http):
    r = http.post(URL, headers=http.session, json={"text": "  Call   the dentist ", "due_at": at(hours=5)})
    body = r.json()
    assert r.status_code == 201 and body["kind"] == "timed" and body["text"] == "Call the dentist"
    assert body["state"] == "pending" and body["explicit_time"] is False and body["expires_at"] is None
    assert [p["key"] for p in body["presets"]][0] == "hour" and "tomorrow" in [p["key"] for p in body["presets"]]


def test_a_note_has_no_time_no_presets_and_an_expiry(http):
    body = make(http, "Look into a standing desk")
    assert body["kind"] == "note" and body["due_at"] is None and body["presets"] == []
    expires = datetime.fromisoformat(body["expires_at"].replace("Z", "+00:00"))
    assert timedelta(days=29) < expires - datetime.now(timezone.utc) < timedelta(days=31)


def test_the_same_reminder_twice_makes_one(http):
    first = make(http, due_at=at(hours=5))
    r = http.post(URL, headers=http.session, json={"text": "call the DENTIST!", "due_at": at(hours=5, minutes=10)})
    assert r.status_code == 200 and r.json()["duplicate"] is True and r.json()["id"] == first["id"]
    assert len(http.get(URL, headers=http.session).json()["reminders"]) == 1
    # a different time is a different reminder
    assert http.post(URL, headers=http.session, json={"text": "Call the dentist", "due_at": at(days=2)}).status_code == 201


@pytest.mark.parametrize("bad", [
    {"due_at": "2026-11-23T10:00:00"},                      # no UTC offset
    {"due_at": at(hours=-3)},                                # an agent with the wrong date
    {"due_at": at(days=900)},
    {"explicit_time": True},                                 # an explicit time needs a time
    {"text": "   "},
    {"source": {"kind": "telepathy"}},
])
def test_nonsense_is_a_422_and_stores_nothing(http, bad):
    body = {"text": "x", **bad}
    assert http.post(URL, headers=http.session, json=body).status_code == 422
    assert http.get(URL, headers=http.session).json()["reminders"] == []


def test_listing_is_soonest_first_notes_last_and_open_includes_fired(http):
    make(http, "later one", due_at=at(hours=9))
    make(http, "a note")
    make(http, "sooner one", due_at=at(hours=3))
    make(http, "due now", due_at=at(minutes=-5))
    fire(http)
    assert [r["text"] for r in http.get(URL, headers=http.session).json()["reminders"]] == ["sooner one", "later one", "a note"]
    opened = http.get(f"{URL}?state=open", headers=http.session).json()["reminders"]
    assert [r["text"] for r in opened] == ["due now", "sooner one", "later one", "a note"]
    assert http.get(f"{URL}?state=bogus", headers=http.session).status_code == 422


# ── firing ───────────────────────────────────────────────────────────

def test_only_what_is_due_fires_and_it_fires_as_an_alert_with_a_push(http, pushes):
    due = make(http, "Pay the water bill", due_at=at(minutes=-5), source={"kind": "chat", "crew": "default"})
    make(http, "Next week thing", due_at=at(days=7))
    make(http, "Just a note")
    result = fire(http)
    assert [r["id"] for r in result["fired"]] == [due["id"]] and result["held"] == 0
    [alert] = alerts(http)
    assert alert["kind"] == "reminder" and alert["title"] == "Pay the water bill" and alert["crew"] == "default"
    assert alert["status"] == "new" and alert["severity"] == "info" and alert["data"]["reminder_id"] == due["id"]
    assert [p["title"] for p in pushes] == ["Pay the water bill"]
    got = http.get(f"{URL}/{due['id']}", headers=http.session).json()
    assert got["state"] == "fired" and got["alert_id"] == alert["id"] and got["late"] is False and got["presets"]


def test_firing_twice_fires_once(http, pushes):
    make(http, due_at=at(minutes=-1))
    assert len(fire(http)["fired"]) == 1
    assert fire(http)["fired"] == [] and len(pushes) == 1 and len(alerts(http)) == 1


def test_an_explicit_time_is_a_warn_and_a_late_one_says_when_it_was_due(http):
    make(http, "Take the pills", due_at=at(minutes=-50), explicit_time=True)
    fire(http)
    [alert] = alerts(http)
    assert alert["severity"] == "warn" and alert["body"].startswith("Was due ")
    assert http.get(f"{URL}?state=fired", headers=http.session).json()["reminders"][0]["late"] is True


def _quiet_now(http, **extra):
    hour = datetime.now(timezone.utc).hour
    body = {"timezone": "UTC", "quiet_start_hour": hour, "quiet_end_hour": (hour + 1) % 24, **extra}
    assert http.put("/api/heartbeat/settings", headers=http.session, json=body).status_code == 200


def test_quiet_hours_hold_a_vague_reminder_but_not_one_the_person_named(http):
    _quiet_now(http)
    vague = make(http, "Tidy the garage", due_at=at(minutes=-5))
    named = make(http, "Leave for the airport", due_at=at(minutes=-5), explicit_time=True)
    result = fire(http)
    assert [r["id"] for r in result["fired"]] == [named["id"]] and result["held"] == 1
    http.put("/api/heartbeat/settings", headers=http.session, json={"quiet_start_hour": 3, "quiet_end_hour": 3})
    assert [r["id"] for r in fire(http)["fired"]] == [vague["id"]]  # no quiet hours now: it goes


def test_a_pause_holds_a_vague_reminder_until_it_ends(http):
    http.put("/api/heartbeat/settings", headers=http.session, json={"paused_until": at(hours=3), "quiet_start_hour": 3, "quiet_end_hour": 3})
    make(http, due_at=at(minutes=-5))
    assert fire(http) == {"fired": [], "held": 1, "expired": 0}
    http.put("/api/heartbeat/settings", headers=http.session, json={"paused_until": None})
    assert len(fire(http)["fired"]) == 1


def test_a_note_that_runs_out_is_dropped_not_fired(http, pushes):
    make(http, "Stale idea", expires_at=at(seconds=-30))
    make(http, "Fresh idea")
    assert fire(http)["expired"] == 1 and pushes == []
    assert [r["text"] for r in http.get(URL, headers=http.session).json()["reminders"]] == ["Fresh idea"]
    assert [r["text"] for r in http.get(f"{URL}?state=expired", headers=http.session).json()["reminders"]] == ["Stale idea"]


# ── changing them ────────────────────────────────────────────────────

def test_putting_a_fired_reminder_off_settles_its_alert_and_it_comes_back_as_the_same_alert(http, pushes):
    made = make(http, "Renew the licence", due_at=at(minutes=-5))
    fire(http)
    [first] = alerts(http)
    r = http.patch(f"{URL}/{made['id']}", headers=http.session, json={"due_at": at(minutes=-1)})
    assert r.status_code == 200 and r.json()["state"] == "pending" and r.json()["fired_at"] is None
    assert [a["status"] for a in alerts(http)] == ["done_by_gaca"]  # the inbox is not nagging about it meanwhile
    fire(http)
    [again] = alerts(http)
    assert again["id"] == first["id"] and again["status"] == "new" and len(pushes) == 2


def test_cancel_and_done_and_the_states_that_cannot_move(http):
    one = make(http, "One", due_at=at(hours=3))
    assert http.patch(f"{URL}/{one['id']}", headers=http.session, json={"state": "cancelled"}).json()["state"] == "cancelled"
    assert http.patch(f"{URL}/{one['id']}", headers=http.session, json={"text": "again"}).status_code == 409
    assert http.get(URL, headers=http.session).json()["reminders"] == []
    two = make(http, "Two", due_at=at(minutes=-1))
    fire(http)
    assert http.patch(f"{URL}/{two['id']}", headers=http.session, json={"state": "cancelled"}).status_code == 409
    assert http.patch(f"{URL}/{two['id']}", headers=http.session, json={"state": "done"}).json()["state"] == "done"


def test_an_unknown_reminder_is_a_404(http):
    assert http.get(f"{URL}/nope", headers=http.session).status_code == 404
    assert http.patch(f"{URL}/nope", headers=http.session, json={"state": "done"}).status_code == 404


def test_a_bad_new_time_is_refused_and_changes_nothing(http):
    made = make(http, due_at=at(hours=3))
    assert http.patch(f"{URL}/{made['id']}", headers=http.session, json={"due_at": at(days=-3)}).status_code == 422
    assert http.get(f"{URL}/{made['id']}", headers=http.session).json()["due_at"] == made["due_at"]


# ── the agent's token ────────────────────────────────────────────────

def test_an_agent_with_the_reminders_scope_can_do_all_of_it(http):
    agent = bearer(mint(http, "hermes-r", ["reminders"])["token"])
    made = http.post(URL, headers=agent, json={"text": "From an agent", "due_at": at(minutes=-2), "source": {"kind": "agent", "crew": "default"}})
    assert made.status_code == 201
    assert len(fire(http, agent)["fired"]) == 1
    assert http.patch(f"{URL}/{made.json()['id']}", headers=agent, json={"state": "done"}).status_code == 200
    assert http.get(URL, headers=agent).status_code == 200


def test_a_token_without_the_scope_is_refused(http):
    other = bearer(mint(http, "alerts-only", ["alerts"])["token"])
    assert http.get(URL, headers=other).status_code == 403
    assert http.post(f"{URL}/fire-due", headers=other).status_code == 403
    assert http.post(URL, headers=other, json={"text": "x"}).status_code == 403


def test_the_scope_is_known_and_part_of_the_default_set():
    from gaca_api.utils import tokens as token_utils
    assert "reminders" in token_utils.SCOPES and "reminders" in token_utils.DEFAULT_SCOPES


# ── the database claim ───────────────────────────────────────────────

def test_a_reminder_can_be_claimed_only_once(tmp_path, monkeypatch):
    from gaca_api.config import settings
    from gaca_api.db import client, reminders as reminders_db
    from gaca_api.models.reminder import ReminderDoc

    monkeypatch.setattr(settings, "db_path", str(tmp_path / "claim.db"))

    async def go():
        await client.connect()
        try:
            original = ReminderDoc(user_id="u", text="x", due_at=datetime.now(timezone.utc))
            await reminders_db.insert(original)
            fired = original.model_copy(update={"state": "fired"})
            assert await reminders_db.claim(original, fired) is True
            assert await reminders_db.claim(original, fired) is False  # a second, overlapping run loses
            await reminders_db.release(fired, original)  # an alert that could not be made hands it back
            assert (await reminders_db.find(original.id)).state == "pending"
            assert await reminders_db.claim(original, fired) is True
        finally:
            await client.close()

    asyncio.run(go())
