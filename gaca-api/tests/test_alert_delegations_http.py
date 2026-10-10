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
    async def start_run(self, *, session_id, history, user_message, images=()):
        return "run_http"

    async def stream_events(self, run_id, *, session_id):
        yield RunCompleted(session_id=session_id, run_id=run_id, output="done")


@pytest.fixture
def http(tmp_path, monkeypatch):
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
    assert alert["status"] == "delegated" and alert["session_id"]
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
