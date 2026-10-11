"""A fresh install has a crew without anyone running a discovery step: the first time the app asks for the
crew, the Hermes profiles on this machine (the `default` one first) are registered."""
import json
import sqlite3
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from gaca_api.config import settings
from gaca_api.main import app
from gaca_api.models.base import utcnow
from gaca_api.provisioning.local import LocalHermes
from gaca_api.services import pairing

CODE = "ABCD2345EFGH6789"
KEY = "d" * 32


@pytest.fixture
def box(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    (home / ".env").write_text(f"OPENAI_API_KEY=keep\nAPI_SERVER_ENABLED=true\nAPI_SERVER_KEY={KEY}\n")  # no port, as the README says
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "t.db"))
    monkeypatch.setattr(settings, "agent_engine", "hermes")
    monkeypatch.setattr(settings, "agent_access", "local")
    monkeypatch.setattr(settings, "hermes_home", str(home))
    monkeypatch.setattr(settings, "hermes_bin", "hermes")
    with TestClient(app) as http:
        http.db, http.home = str(tmp_path / "t.db"), home
        state = {"hash": pairing._digest(CODE), "expires_at": (utcnow() + timedelta(minutes=10)).isoformat(), "wrong": 0}
        con = sqlite3.connect(http.db)
        con.execute("INSERT OR REPLACE INTO server_meta (key, value) VALUES ('pair_code', ?)", (json.dumps(state),))
        con.commit()
        con.close()
        made = http.post("/api/devices/pair", json={"code": CODE, "name": "Phone"}).json()
        http.h = {"Authorization": "Bearer " + made["access_token"]}
        yield http


def test_the_first_look_at_the_crew_registers_the_default_profile(box):
    crew = box.get("/api/crew", headers=box.h).json()
    assert crew["host"] == "http://127.0.0.1"
    assert [(m["name"], m["is_default"]) for m in crew["crew"]] == [("default", True)]
    assert KEY not in json.dumps(crew)  # keys are never returned


def test_it_is_remembered_and_not_redone(box):
    first = box.get("/api/crew", headers=box.h).json()
    con = sqlite3.connect(box.db)
    stored = json.loads(con.execute("SELECT doc FROM users").fetchone()[0])
    con.close()
    assert [m["name"] for m in stored["crew"]] == ["default"] and stored["default_crew"] == "default"
    assert box.get("/api/crew", headers=box.h).json() == first


def test_a_profile_made_later_shows_up_on_the_next_look(box):
    box.get("/api/crew", headers=box.h)
    (box.home / "profiles" / "research").mkdir(parents=True)
    (box.home / "profiles" / "research" / ".env").write_text("API_SERVER_ENABLED=true\nAPI_SERVER_PORT=8650\nAPI_SERVER_KEY=" + "r" * 32 + "\n")
    names = [m["name"] for m in box.get("/api/crew", headers=box.h).json()["crew"]]
    assert names == ["default", "research"]


def test_without_an_enabled_api_server_the_crew_is_simply_empty(box):
    (box.home / ".env").write_text("OPENAI_API_KEY=keep\n")
    crew = box.get("/api/crew", headers=box.h).json()
    assert crew["crew"] == [] and box.get("/api/crew", headers=box.h).status_code == 200


def test_a_hermes_that_cannot_be_read_does_not_break_the_crew_endpoint(box, monkeypatch):
    async def boom(self):
        raise OSError("unreadable")
    monkeypatch.setattr(LocalHermes, "discover_profiles", boom)
    assert box.get("/api/crew", headers=box.h).status_code == 200
