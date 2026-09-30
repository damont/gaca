"""HTTP-level checks for /api/briefings: routing, validation, per-user scoping."""
import pytest
from fastapi.testclient import TestClient

from gaca_api.config import settings
from gaca_api.main import app
from gaca_api.models.user import UserDoc
from gaca_api.utils.auth import get_current_user

_current = {"id": "u1"}


@pytest.fixture
def http(tmp_path):
    settings.db_path = str(tmp_path / "t.db")
    app.dependency_overrides[get_current_user] = lambda: UserDoc(
        id=_current["id"], email=f"{_current['id']}@example.com", display_name="Test"
    )
    _current["id"] = "u1"
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


SECTION = {
    "title": "Calendar",
    "kind": "calendar",
    "headline": "2 things today",
    "items": [{"title": "Soccer", "time": "5:00 PM", "priority": "high",
               "link": "https://calendar.example/#week-1"}],
}


def test_put_then_get_round_trip(http):
    r = http.put("/api/briefings/2026-10-01/sections/calendar", json=SECTION)
    assert r.status_code == 200 and r.json()["key"] == "calendar"
    http.put("/api/briefings/2026-10-01", json={"summary": "Busy day"})

    body = http.get("/api/briefings", params={"date": "2026-10-01"}).json()
    assert body["date"] == "2026-10-01" and body["summary"] == "Busy day"
    assert body["sections"][0]["items"][0]["title"] == "Soccer"
    assert http.get("/api/briefings/dates").json() == {"dates": ["2026-10-01"]}


def test_rejects_bad_key_date_and_link(http):
    assert http.put("/api/briefings/2026-10-01/sections/Bad Key!", json=SECTION).status_code == 422
    assert http.put("/api/briefings/not-a-date/sections/calendar", json=SECTION).status_code == 422
    bad = {**SECTION, "items": [{"title": "x", "link": "javascript:alert(1)"}]}
    assert http.put("/api/briefings/2026-10-01/sections/calendar", json=bad).status_code == 422


def test_delete_missing_section_is_404_and_users_are_isolated(http):
    assert http.delete("/api/briefings/2026-10-01/sections/calendar").status_code == 404
    http.put("/api/briefings/2026-10-01/sections/calendar", json=SECTION)
    _current["id"] = "u2"
    assert http.get("/api/briefings", params={"date": "2026-10-01"}).json()["sections"] == []
    assert http.delete("/api/briefings/2026-10-01/sections/calendar").status_code == 404
    _current["id"] = "u1"
    assert http.delete("/api/briefings/2026-10-01/sections/calendar").status_code == 204
