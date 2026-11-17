"""HTTP-level checks for POST /api/alerts/delegations and the derived AlertOut fields."""
import pytest
from fastapi.testclient import TestClient

import gaca_api.agent.runtime as agent_runtime
from gaca_api.config import settings
from gaca_api.main import app
from gaca_api.models.user import CrewMember, UserDoc
from gaca_api.schemas.event import RunCompleted
from gaca_api.utils.auth import get_current_user


class _Backend:
    sent: list[str] = []

    async def start_run(self, *, session_id, history, user_message, images=()):
        self.sent.append(user_message)
        return "run_http"

    async def stream_events(self, run_id, *, session_id):
        yield RunCompleted(session_id=session_id, run_id=run_id, output="done")


@pytest.fixture
def http(tmp_path, monkeypatch):
    _Backend.sent = []
    settings.db_path = str(tmp_path / "t.db")
    monkeypatch.setattr(settings, "agent_engine", "hermes")
    monkeypatch.setattr(agent_runtime, "backend_for", lambda base_url, api_key: _Backend())
    app.dependency_overrides[get_current_user] = lambda: UserDoc(
        id="u1", email="u1@example.com", display_name="T", agent_host="http://agent.example.com",
        crew=[CrewMember(name="hermes", port=8642, api_key="k")], default_crew="hermes",
    )
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_one_call_delegation(http):
    r = http.post("/api/alerts/delegations", json={"instruction": "Renew the domain\nbefore Friday"})
    assert r.status_code == 201, r.text
    alert = r.json()
    assert alert["status"] == "new" and alert["open"] and alert["session_id"]
    assert [r["state"] for r in alert["runs"]] == ["running"]
    assert (alert["kind"], alert["title"], alert["crew"], alert["source"]) == (
        "manual.delegation", "Renew the domain", "hermes", "app")
    assert alert["data_rows"] == [] and alert["artifact"] is None

    assert http.post("/api/alerts/delegations", json={"instruction": "   "}).status_code == 422
    assert http.post("/api/alerts/delegations", json={"instruction": "x", "crew": "nobody"}).status_code == 422


def test_list_and_detail_carry_data_rows_and_artifact(http):
    page = http.post("/api/artifacts", json={"title": "Week", "html": "<p>"}).json()
    made = http.post("/api/alerts", json={
        "kind": "k", "title": "t", "data": {"used_pct": 91, "mount": "/"}, "artifact_id": page["id"],
    }).json()
    detail = http.get(f"/api/alerts/{made['id']}").json()
    listed = http.get("/api/alerts").json()["alerts"][0]
    for alert in (made, detail, listed):
        assert alert["data_rows"] == [{"key": "used_pct", "value": "91"}, {"key": "mount", "value": "/"}]
        assert alert["artifact"] == {
            "id": page["id"], "title": "Week", "description": None, "crew": None, "missing": False}


def test_sessions_carry_last_activity_at_everywhere(http):
    created = http.post("/api/sessions", json={}).json()
    assert created["last_activity_at"] == created["created_at"]
    assert http.get("/api/sessions").json()["sessions"][0]["last_activity_at"] == created["created_at"]
    assert http.get(f"/api/sessions/{created['id']}").json()["last_activity_at"] == created["created_at"]


# ── actions, closing, and the chat that carries the alert ────────────────────

def _post_alert(http, **over):
    body = {"kind": "email.event", "title": "Dentist Thursday 3pm", "body": "From the clinic",
            "suggested_actions": [
                {"label": "Add to calendar", "prompt": "Add 'Dentist, Thu 3pm' to the calendar", "kind": "calendar.add"},
                {"label": "Draft a reply", "prompt": "Draft a reply confirming", "kind": "email.draft"}],
            **over}
    return http.post("/api/alerts", json=body).json()


def _settle(http, alert_id):
    """Wait for the background run reader, then read the alert."""
    import asyncio
    from gaca_api.services import runs
    http.portal.call(lambda: asyncio.gather(*list(runs._background)))
    return http.get(f"/api/alerts/{alert_id}").json()


def test_tapping_an_action_runs_it_and_closes_the_alert_done_by_gaca(http):
    alert = _post_alert(http)
    r = http.post(f"/api/alerts/{alert['id']}/actions/0/run")
    assert r.status_code == 200, r.text
    started = r.json()
    assert started["open"] and [run["state"] for run in started["runs"]] == ["running"]
    assert started["runs"][0]["label"] == "Add to calendar"

    done = _settle(http, alert["id"])
    assert done["status"] == "done_by_gaca" and done["open"] is False
    assert done["runs"][0]["state"] == "done" and done["runs"][0]["summary"] == "done"
    # The agent was given the action's own prompt, plus the alert (all of it), plus the answer contract.
    sent = _Backend.sent[0]
    assert "Add 'Dentist, Thu 3pm' to the calendar" in sent
    assert "Dentist Thursday 3pm" in sent and "From the clinic" in sent and "Draft a reply" in sent
    assert "at most three short lines" in sent
    # The chat records only what the action said, not the wrapper.
    messages = http.get(f"/api/sessions/{done['session_id']}").json()["messages"]
    assert [m["content"] for m in messages if m["role"] == "user"] == ["Add 'Dentist, Thu 3pm' to the calendar"]


def test_an_action_that_does_not_exist_is_a_422_and_a_closed_alert_a_409(http):
    alert = _post_alert(http)
    assert http.post(f"/api/alerts/{alert['id']}/actions/5/run").status_code == 422
    assert http.post(f"/api/alerts/{alert['id']}/close", json={"reason": "noted"}).status_code == 200
    assert http.post(f"/api/alerts/{alert['id']}/actions/0/run").status_code == 409


def test_closing_takes_one_of_three_reasons(http):
    alert = _post_alert(http)
    assert http.post(f"/api/alerts/{alert['id']}/close", json={"reason": "done_by_gaca"}).status_code == 422
    assert http.post(f"/api/alerts/{alert['id']}/close", json={}).status_code == 422
    closed = http.post(f"/api/alerts/{alert['id']}/close", json={"reason": "done_by_me", "note": "called them"})
    assert closed.status_code == 200
    assert closed.json()["status"] == "done_by_me" and closed.json()["open"] is False
    assert closed.json()["effective_status"] == "done_by_me"
    assert http.post(f"/api/alerts/{alert['id']}/close", json={"reason": "noted"}).status_code == 409
    listed = http.get("/api/alerts", params={"status": "done_by_me"}).json()["alerts"]
    assert [a["id"] for a in listed] == [alert["id"]]


def test_a_chat_tied_to_an_alert_always_gets_the_alert(http):
    alert = _post_alert(http)
    taken = http.post(f"/api/alerts/{alert['id']}/take-to-chat").json()
    assert taken["draft"] is None and taken["alert"]["status"] == "done_by_gaca"
    sid = taken["session_id"]

    assert http.post(f"/api/sessions/{sid}/messages", json={"content": "just add the first one"}).status_code == 202
    sent = _Backend.sent[-1]
    assert sent.endswith("just add the first one")
    assert "Dentist Thursday 3pm" in sent and "From the clinic" in sent
    # ...but the transcript is only what was typed.
    messages = http.get(f"/api/sessions/{sid}").json()["messages"]
    assert [m["content"] for m in messages if m["role"] == "user"] == ["just add the first one"]

    # An ordinary chat has no such context.
    plain = http.post("/api/sessions", json={}).json()["id"]
    http.post(f"/api/sessions/{plain}/messages", json={"content": "hello"})
    assert _Backend.sent[-1] == "hello"


def test_the_alert_json_carries_what_an_app_needs_to_draw_it_and_nothing_to_decide(http):
    """The contract the apps read: per-action state and the layout come from the API."""
    alert = _post_alert(http)
    assert alert["open"] is True and alert["draft"] is None
    assert [a["index"] for a in alert["actions"]] == [0, 1]
    first = alert["actions"][0]
    assert (first["label"], first["kind"], first["cta"], first["state"], first["can_run"], first["prominent"]) == (
        "Add to calendar", "calendar.add", "Add", "idle", True, False)
    w = alert["workflow"]
    assert (w["phase"], w["actions_heading"]) == ("open", "Hermes can add these")
    assert [(o["reason"], o["primary"]) for o in w["close_options"]] == [
        ("done_by_me", True), ("noted", False), ("not_useful", False)]
    assert w["chat"] == {"hint": "Hermes already has this alert", "prominent": False}

    two = _post_alert(http, title="Two events", suggested_actions=[
        {"label": "Add Soccer", "prompt": "Add soccer", "kind": "calendar.add"},
        {"label": "Add Concert", "prompt": "Add concert", "kind": "calendar.add"}])
    http.post(f"/api/alerts/{two['id']}/actions/0/run")
    done = _settle(http, two["id"])
    assert done["actions"][0]["state"] == "done" and done["actions"][0]["result"]["summary"] == "done"
    assert done["actions"][1]["state"] == "idle"                    # one of two: the alert is still open
    assert done["status"] == "new" and done["workflow"]["progress_label"] == "1 of 2 added"
