"""Crew profile: cron wording, Hermes->neutral translation, adapter resilience, route."""
import asyncio
from datetime import datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from gaca_api.agent.hermes import translate_profile as tp
from gaca_api.agent.hermes.adapter import HermesAdapter
from gaca_api.main import app
from gaca_api.models.user import CrewMember, UserDoc
from gaca_api.services import auth as auth_service
from gaca_api.utils.auth import get_current_user
from gaca_api.utils.cron_text import describe_cron

# Shapes captured live from hermes-agent 0.20.5 (values trimmed).
HEALTH = {
    "status": "ok", "version": "0.20.5", "gateway_state": "running",
    "platforms": {
        "telegram": {"state": "connected", "error_code": None, "needs_attention": False},
        "api_server": {"state": "connected", "error_code": None},
        "discord": {"state": "retrying", "error_code": "auth", "needs_attention": True},
    },
}
OPTIONS = {"model": "gpt-6-luna", "provider": "openai-codex",
           "providers": [{"slug": "nous", "name": "Nous Portal", "is_current": False},
                         {"slug": "openai-codex", "name": "ChatGPT or Codex Subscription", "is_current": True}]}
SKILLS = {"data": [
    {"name": "a", "category": "productivity"}, {"name": "b", "category": "productivity"},
    {"name": "c", "category": "creative"}, {"name": "d", "category": None},
]}
TOOLSETS = {"data": [
    {"name": "web", "label": "🔍 Web Search & Scraping", "description": "web_search, web_extract", "enabled": True},
    {"name": "browser", "label": "🌐 Browser", "description": "x", "enabled": False},
    {"name": "terminal", "label": "💻 Terminal", "description": "shell", "enabled": True},
]}
JOBS = {"jobs": [
    {"id": "j2", "name": "later", "schedule": {"kind": "cron", "expr": "30 9 * * 1", "display": "30 9 * * 1"},
     "enabled": True, "next_run_at": "2026-10-05T09:30:00-04:00", "last_run_at": "2026-09-28T09:30:42-04:00",
     "last_status": "ok", "no_agent": True},
    {"id": "j1", "name": "soon", "schedule": {"kind": "cron", "expr": "0 7 * * *", "display": "0 7 * * *"},
     "enabled": True, "next_run_at": "2026-10-01T07:00:00-04:00", "last_run_at": None, "no_agent": False},
    {"id": "j3", "name": "paused", "schedule": {"kind": "cron", "expr": "5 0,7 * * *"},
     "enabled": True, "paused_at": "2026-09-01T00:00:00", "next_run_at": None},
]}


@pytest.mark.parametrize("expr,text", [
    ("0 7 * * *", "Every day at 7:00 AM"),
    ("5 0,7 * * *", "Every day at 12:05 AM, 7:05 AM"),
    ("30 9 * * 1", "Every Monday at 9:30 AM"),
    ("0 18 * * 1,3,5", "Every Monday, Wednesday, Friday at 6:00 PM"),
    ("*/15 * * * *", "Every 15 minutes"),
    ("0 */2 * * *", "Every 2 hours"),
    ("15 * * * *", "Every hour at :15"),
    ("0 8 15 * *", "Monthly on day 15 at 8:00 AM"),
    ("4 18 8 8 *", "Every August 8 at 6:04 PM"),
    ("0 7 * * 1-5", "0 7 * * 1-5"),  # ranges: fall back to the raw expression
    ("not cron", "not cron"),
])
def test_describe_cron(expr, text):
    assert describe_cron(expr) == text


def test_translation_of_each_hermes_piece():
    assert tp.engine_label(HEALTH) == "Hermes 0.20.5" and tp.is_online(HEALTH)
    ch = {c.name: c for c in tp.channels(HEALTH)}
    assert ch["Telegram"].state == "connected" and not ch["Telegram"].needs_attention
    assert "GACA app" in ch  # api_server renamed for people
    assert ch["Discord"].state == "error" and ch["Discord"].needs_attention

    m = tp.model(OPTIONS)
    assert (m.name, m.provider) == ("gpt-6-luna", "ChatGPT or Codex Subscription")

    count, cats = tp.skill_summary(SKILLS)
    assert count == 4
    assert [(c.name, c.count) for c in cats] == [("productivity", 2), ("creative", 1), ("general", 1)]

    assert [(t.name, t.label) for t in tp.toolsets(TOOLSETS)] == [("web", "Web Search & Scraping"), ("terminal", "Terminal")]

    jobs = tp.jobs(JOBS)
    assert [j.name for j in jobs] == ["soon", "later", "paused"]  # soonest first, unscheduled last
    assert jobs[0].schedule == "Every day at 7:00 AM" and jobs[0].kind == "agent"
    assert jobs[1].kind == "script" and jobs[1].last_status == "ok"
    assert jobs[2].enabled is False
    assert isinstance(jobs[0].next_run, datetime)


def test_translation_tolerates_missing_and_odd_fields():
    assert tp.channels({}) == [] and tp.toolsets({}) == [] and tp.jobs({}) == []
    assert tp.model({}) is None
    assert tp.skill_summary({}) == (0, [])
    assert tp.jobs({"jobs": [{"id": "x"}, "junk", {"name": "no id"}]})[0].name == "x"


def _adapter(routes: dict[str, object]) -> HermesAdapter:
    def handler(request: httpx.Request) -> httpx.Response:
        result = routes.get(request.url.path)
        if isinstance(result, Exception) or result is None:
            raise httpx.ConnectError("down", request=request)
        if isinstance(result, int):
            return httpx.Response(result)
        return httpx.Response(200, json=result)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return HermesAdapter("http://engine:8642", "k" * 16, client=client)


FULL = {"/health/detailed": HEALTH, "/api/model/options": OPTIONS, "/v1/skills": SKILLS,
        "/v1/toolsets": TOOLSETS, "/api/jobs": JOBS}


def test_describe_profile_assembles_everything():
    info = asyncio.run(_adapter(FULL).describe_profile())
    assert info.status == "online" and info.engine == "Hermes 0.20.5"
    assert info.model.name == "gpt-6-luna" and info.skill_count == 4
    assert len(info.toolsets) == 2 and len(info.jobs) == 3 and info.unavailable == []


def test_describe_profile_one_failing_piece_only_blanks_that_piece():
    info = asyncio.run(_adapter({**FULL, "/api/jobs": 500}).describe_profile())
    assert info.status == "online" and info.jobs == [] and info.unavailable == ["scheduled jobs"]
    assert info.skill_count == 4  # the rest is intact


def test_describe_profile_when_hermes_is_down_is_offline_not_an_error():
    info = asyncio.run(_adapter({}).describe_profile())
    assert info.status == "offline" and len(info.unavailable) == 5


_current = {"id": "u1"}


@pytest.fixture
def http(tmp_path, monkeypatch):
    from gaca_api.config import settings
    settings.db_path = str(tmp_path / "t.db")
    adapter = _adapter(FULL)
    monkeypatch.setattr(auth_service, "backend_for_crew", lambda user, member: adapter)
    app.dependency_overrides[get_current_user] = lambda: UserDoc(
        id=_current["id"], email="u@example.com", display_name="T", agent_host="h",
        crew=[CrewMember(name="default", port=1, api_key="k", color="blue"),
              CrewMember(name="trading", port=2, api_key="k2", color="amber")],
        default_crew="default",
    )
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_profile_route_joins_gaca_identity_with_the_engine_picture(http):
    r = http.get("/api/crew/trading/profile")
    assert r.status_code == 200
    body = r.json()
    assert (body["name"], body["color"], body["is_default"]) == ("trading", "amber", False)
    assert body["status"] == "online" and body["model"]["name"] == "gpt-6-luna"
    assert body["jobs"][0]["schedule"] == "Every day at 7:00 AM"
    assert "api_key" not in r.text and "port" not in body  # internals never leak
    assert http.get("/api/crew/default/profile").json()["is_default"] is True
    assert http.get("/api/crew/nobody/profile").status_code == 404


# ── GET /crew/{name}/skills: Hermes's trouble is a 502 with a reason, never a 500 ──

def _skills_route(http, monkeypatch, routes):
    adapter = _adapter(routes)
    monkeypatch.setattr(auth_service, "backend_for_crew", lambda user, member: adapter)
    return http.get("/api/crew/default/skills")


def test_skills_route_lists_hermes_skills(http):
    r = http.get("/api/crew/default/skills")
    assert r.status_code == 200 and [s["name"] for s in r.json()] == ["a", "b", "c", "d"]


def test_a_skill_without_a_description_still_lists(http, monkeypatch):
    r = _skills_route(http, monkeypatch, {"/v1/skills": {"data": [{"name": "x", "description": None}]}})
    assert r.status_code == 200 and r.json() == [{"name": "x", "description": "", "category": None, "use_count": 0, "last_used_at": None}]


def test_hermes_error_on_skills_is_a_502_with_a_reason(http, monkeypatch):
    r = _skills_route(http, monkeypatch, {"/v1/skills": 500})
    assert r.status_code == 502 and "500" in r.json()["detail"]


def test_hermes_down_on_skills_is_a_502_with_a_reason(http, monkeypatch):
    r = _skills_route(http, monkeypatch, {})
    assert r.status_code == 502 and "Couldn't reach Hermes" in r.json()["detail"]
