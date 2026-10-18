"""The skills fallback: when Hermes' GET /v1/skills fails (it 500s on every release from
v2026.9.11 to at least v0.21.7), gaca-api reads the profile's skills/ folder itself."""
import asyncio
import json
import os
import subprocess
import sys

import httpx
import pytest
from fastapi.testclient import TestClient

from gaca_api.agent.hermes import translate_skills as tsk
from gaca_api.agent.hermes.adapter import HermesAdapter
from gaca_api.main import app
from gaca_api.models.user import CrewMember, UserDoc
from gaca_api.provisioning import ssh
from gaca_api.provisioning.local import LocalHermes
from gaca_api.services import auth as auth_service
from gaca_api.utils.auth import get_current_user


def _skill(root, *parts, text):
    folder = root.joinpath(*parts)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(text)


def _fake_hermes(tmp_path):
    home = tmp_path / ".hermes"
    skills = home / "skills"
    _skill(skills, "gaca", "gaca-alerts", text="---\nname: gaca-alerts\ndescription: Post alerts to GACA.\n---\n# x\n")
    _skill(skills, "devops", "sdlc-review", text='---\nname: sdlc-review\ndescription: "Review handoffs."\nmetadata:\n  a: 1\n---\nbody\n')
    _skill(skills, "loose", text="# Loose\n\nA skill with no frontmatter.\n")
    _skill(skills, "gaca", "gaca-alerts", "references", text="---\nname: not-a-skill\n---\n")  # inside a skill
    _skill(skills, ".hub", "cached", text="---\nname: hidden\n---\n")
    (tmp_path / "elsewhere").mkdir()
    _skill(tmp_path / "elsewhere", "planted", text="---\nname: planted\n---\n")
    (skills / "linked").symlink_to(tmp_path / "elsewhere")
    _skill(home / "profiles" / "toolshed" / "skills", "tools", "saw", text="---\nname: saw\ndescription: Cuts.\n---\n")
    return home


EXPECTED_DEFAULT = [
    {"name": "sdlc-review", "description": "Review handoffs.", "category": "devops"},
    {"name": "gaca-alerts", "description": "Post alerts to GACA.", "category": "gaca"},
    {"name": "loose", "description": "A skill with no frontmatter.", "category": None},
]


def _sorted(skills):
    return sorted(skills, key=lambda s: (s["category"] or "", s["name"]))


def _with_usage(skills):
    return [{**s, "use_count": 0, "last_used_at": None} for s in skills]


def test_translate_reads_frontmatter_falls_back_to_body_and_folder():
    s = tsk.to_skill_info(["loose"], "# Title\n\nFirst line.\n")
    assert (s.name, s.description, s.category) == ("loose", "First line.", None)
    s = tsk.to_skill_info(["cat", "x"], "---\nname: real\ndescription: 'Quoted'\n---\nbody")
    assert (s.name, s.description, s.category) == ("real", "Quoted", "cat")


def test_local_reader_finds_skills_and_never_leaves_the_folder(tmp_path):
    hermes = LocalHermes(_fake_hermes(tmp_path), "hermes")
    files = asyncio.run(hermes.read_skill_files("default"))
    assert [s.model_dump() for s in tsk.to_skills(files)] == _with_usage(_sorted(EXPECTED_DEFAULT))
    assert [s.name for s in tsk.to_skills(asyncio.run(hermes.read_skill_files("toolshed")))] == ["saw"]
    assert asyncio.run(hermes.read_skill_files("ghost")) is None
    for hostile in ("../x", "a/b", "$(id)"):
        with pytest.raises(Exception):
            asyncio.run(hermes.read_skill_files(hostile))


def _run_script(home, name):
    out = subprocess.run([sys.executable, "-c", ssh._READ_SKILLS_SCRIPT, name],
                         env={**os.environ, "HOME": str(home)}, capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def test_on_box_script_matches_the_local_reader(tmp_path):
    _fake_hermes(tmp_path)
    found = _run_script(tmp_path, "default")
    assert [s.model_dump() for s in tsk.to_skills(found["skills"])] == _with_usage(_sorted(EXPECTED_DEFAULT))
    for hostile in ("ghost", "../x", "..", "a/b", "$(id)", ""):
        assert _run_script(tmp_path, hostile) == {"found": False}, hostile


# ── the route: Hermes' 500 falls back to the files; no box access keeps the 502 ──

@pytest.fixture
def http(tmp_path, monkeypatch):
    from gaca_api.config import settings
    settings.db_path = str(tmp_path / "t.db")
    monkeypatch.setattr(settings, "fcm_credentials_path", None)  # don't start Firebase for a skills test
    broken = HermesAdapter("http://engine:8642", "k" * 16, client=httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(500))))
    monkeypatch.setattr(auth_service, "backend_for_crew", lambda user, member: broken)
    app.dependency_overrides[get_current_user] = lambda: UserDoc(
        id="u1", email="u@example.com", display_name="T", agent_host="h",
        crew=[CrewMember(name="default", port=1, api_key="k", color="blue")], default_crew="default")
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_hermes_500_falls_back_to_the_skill_files(http, tmp_path, monkeypatch):
    hermes = LocalHermes(_fake_hermes(tmp_path), "hermes")
    monkeypatch.setattr(auth_service, "provisioner_for", lambda user: hermes)
    r = http.get("/api/crew/default/skills")
    assert r.status_code == 200 and r.json() == _with_usage(_sorted(EXPECTED_DEFAULT))


def test_without_box_access_the_hermes_error_stands(http, monkeypatch):
    monkeypatch.setattr(auth_service, "provisioner_for", lambda user: None)
    r = http.get("/api/crew/default/skills")
    assert r.status_code == 502 and "500" in r.json()["detail"]


# ── use counts: Hermes keeps them in skills/.usage.json, so they come from the box ──

USAGE = {
    "gaca-alerts": {"use_count": 14, "view_count": 7, "last_used_at": "2026-10-17T08:00:00+00:00", "state": "active"},
    "sdlc-review": {"use_count": 31, "last_used_at": "2026-10-01T08:00:00+00:00"},
    "loose": {"use_count": 0, "view_count": 9, "last_used_at": None},
    "odd": "not a record",
}


def test_translate_usage_is_forgiving():
    got = tsk.to_usage(json.dumps(USAGE))
    assert got["sdlc-review"].use_count == 31 and got["gaca-alerts"].last_used_at.year == 2026
    assert got["loose"].use_count == 0 and "odd" not in got
    bad = json.dumps({"x": {"use_count": True, "last_used_at": "yesterday"}, "y": {"use_count": -3}})
    assert tsk.to_usage(bad)["x"].use_count == 0 and tsk.to_usage(bad)["x"].last_used_at is None
    assert tsk.to_usage(bad)["y"].use_count == 0
    for junk in (None, "", "{", "[]", "3"):
        assert tsk.to_usage(junk) == {}


def test_local_usage_reader_stays_inside_the_profile(tmp_path):
    home = _fake_hermes(tmp_path)
    hermes = LocalHermes(home, "hermes")
    assert asyncio.run(hermes.read_skill_usage("default")) is None  # no file yet
    (home / "skills" / ".usage.json").write_text(json.dumps(USAGE))
    assert json.loads(asyncio.run(hermes.read_skill_usage("default")))["sdlc-review"]["use_count"] == 31
    assert asyncio.run(hermes.read_skill_usage("toolshed")) is None
    assert asyncio.run(hermes.read_skill_usage("ghost")) is None
    # a symlinked counter file is never followed
    secret = tmp_path / "secret.json"
    secret.write_text('{"x": 1}')
    (home / "profiles" / "toolshed" / "skills" / ".usage.json").symlink_to(secret)
    assert asyncio.run(hermes.read_skill_usage("toolshed")) is None
    for hostile in ("../x", "a/b", "$(id)"):
        with pytest.raises(Exception):
            asyncio.run(hermes.read_skill_usage(hostile))


def test_on_box_usage_script_matches_the_local_reader(tmp_path):
    home = _fake_hermes(tmp_path)
    (home / "skills" / ".usage.json").write_text(json.dumps(USAGE))

    def run(name):
        out = subprocess.run([sys.executable, "-c", ssh._READ_SKILL_USAGE_SCRIPT, name],
                             env={**os.environ, "HOME": str(tmp_path)}, capture_output=True, text=True, check=True).stdout
        return json.loads(out)["text"]

    assert json.loads(run("default")) == USAGE
    for missing in ("toolshed", "ghost", "../x", "..", "a/b", "$(id)", ""):
        assert run(missing) is None, missing


def test_skills_route_carries_counts_and_ranks_by_them(http, tmp_path, monkeypatch):
    home = _fake_hermes(tmp_path)
    (home / "skills" / ".usage.json").write_text(json.dumps(USAGE))
    monkeypatch.setattr(auth_service, "provisioner_for", lambda user: LocalHermes(home, "hermes"))
    by_name = {s["name"]: s for s in http.get("/api/crew/default/skills").json()}
    assert by_name["sdlc-review"]["use_count"] == 31
    assert by_name["gaca-alerts"]["last_used_at"].startswith("2026-10-17")
    assert by_name["loose"]["use_count"] == 0 and by_name["loose"]["last_used_at"] is None
    top = http.get("/api/crew/default/skills", params={"sort": "popular", "limit": 2}).json()
    assert [s["name"] for s in top] == ["sdlc-review", "gaca-alerts"]
    assert [s["name"] for s in http.get("/api/crew/default/skills", params={"sort": "popular"}).json()][-1] == "loose"
    assert http.get("/api/crew/default/skills", params={"sort": "bogus"}).status_code == 422
    assert http.get("/api/crew/default/skills", params={"limit": 0}).status_code == 422


def test_unreadable_usage_leaves_the_list_working(http, tmp_path, monkeypatch):
    home = _fake_hermes(tmp_path)
    (home / "skills" / ".usage.json").write_text("{not json")
    monkeypatch.setattr(auth_service, "provisioner_for", lambda user: LocalHermes(home, "hermes"))
    r = http.get("/api/crew/default/skills", params={"sort": "popular"})
    assert r.status_code == 200 and len(r.json()) == 3 and all(s["use_count"] == 0 for s in r.json())
