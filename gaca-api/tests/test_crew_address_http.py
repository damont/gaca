"""HTTP-level checks that a crew member's address path is shown, accepted and validated.
(That an edit which doesn't mention `path` keeps it is covered where it's implemented:
tests/services/test_auth.py.)"""
import pytest
from fastapi.testclient import TestClient

from gaca_api.config import settings
from gaca_api.main import app
from gaca_api.models.user import CrewMember, UserDoc
from gaca_api.utils.auth import get_current_user


@pytest.fixture
def http(tmp_path):
    settings.db_path = str(tmp_path / "t.db")
    user = UserDoc(
        id="u1", email="u@example.com", display_name="T", agent_host="http://box",
        crew=[CrewMember(name="default", port=8642, api_key="dk"),
              CrewMember(name="toolshed", port=8643, api_key="tk")],
        default_crew="default",
    )
    app.dependency_overrides[get_current_user] = lambda: user
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


def test_crew_list_shows_each_members_path_and_never_a_key(http):
    response = http.get("/api/crew")
    assert {c["name"]: (c["port"], c["path"]) for c in response.json()["crew"]} == {
        "default": (8642, ""), "toolshed": (8643, ""),
    }
    assert "api_key" not in response.text and "tk" not in response.text.replace("toolshed", "")


def test_a_bad_path_is_a_422_not_a_500(http):
    for bad in ("p/toolshed", "/p/../x", "http://evil/x", "/p/a b", "//x"):
        r = http.put("/api/crew/toolshed", json={"port": 8642, "api_key": "tk", "path": bad})
        assert r.status_code == 422, bad
    assert http.post("/api/crew", json={"name": "x", "port": 8642, "api_key": "k", "path": "/p/../x"}).status_code == 422


def test_put_accepts_and_normalizes_a_path(http):
    r = http.put("/api/crew/toolshed", json={"port": 8642, "api_key": "tk", "path": "/p/toolshed/"})
    assert r.status_code == 200
    assert {c["name"]: (c["port"], c["path"]) for c in r.json()["crew"]}["toolshed"] == (8642, "/p/toolshed")
