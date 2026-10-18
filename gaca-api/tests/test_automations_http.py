"""GET /api/automations and the per-automation routes, against a fake Hermes gateway and a fake
Hermes home on disk."""
import json
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from gaca_api.agent.hermes.adapter import HermesAdapter
from gaca_api.main import app
from gaca_api.models.user import CrewMember, UserDoc
from gaca_api.provisioning.local import LocalHermes
from gaca_api.services import auth as auth_service
from gaca_api.utils.auth import get_current_user

BRIEFING, SCOUT, ONCE = "aaaaaaaaaaaa", "bbbbbbbbbbbb", "cccccccccccc"


def job(job_id, name, **kw):
    return {"id": job_id, "name": name, "schedule": {"kind": "cron", "expr": "0 7 * * *"}, "enabled": True,
            "state": "scheduled", "next_run_at": "2026-10-19T07:00:00-04:00", "last_status": "ok",
            "last_run_at": "2026-10-18T07:00:00-04:00", "no_agent": False, **kw}


class Gateway:
    """A tiny stand-in for Hermes' /api/jobs: the jobs, and a record of what was asked."""

    def __init__(self):
        self.jobs = {
            BRIEFING: job(BRIEFING, "Daily briefing"),
            SCOUT: job(SCOUT, "Deal scout", next_run_at="2026-10-18T22:00:00-04:00"),
            ONCE: job(ONCE, "One time", enabled=False, state="completed", next_run_at=None),
        }
        self.calls = []
        self.down = False
        self.refuse_run = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path, dict(request.url.params)))
        if self.down:
            return httpx.Response(500)
        parts = request.url.path.strip("/").split("/")  # api / jobs / [id / [action]]
        if parts[:2] != ["api", "jobs"]:
            return httpx.Response(404)
        if len(parts) == 2:
            wanted = request.url.params.get("include_disabled") == "true"
            return httpx.Response(200, json={"jobs": [j for j in self.jobs.values() if wanted or j["enabled"]]})
        found = self.jobs.get(parts[2])
        if found is None:
            return httpx.Response(404, json={"error": "Job not found"})
        if len(parts) == 4:
            if parts[3] == "run" and self.refuse_run:
                return httpx.Response(400, json={"error": "Cannot run: job is completed (terminal)."})
            if parts[3] == "pause":
                found.update(enabled=False, state="paused")
            elif parts[3] == "resume":
                found.update(enabled=True, state="scheduled")
        return httpx.Response(200, json={"job": found})


def ledger(home):
    cron = home / "cron"
    (cron / "output" / BRIEFING).mkdir(parents=True)
    conn = sqlite3.connect(cron / "executions.db")
    conn.execute("""CREATE TABLE executions (id TEXT PRIMARY KEY, job_id TEXT, source TEXT, process_id TEXT, pid INTEGER,
      process_started_at INTEGER, status TEXT, handoff_pending INTEGER DEFAULT 0, handoff_started_at REAL,
      claimed_at TEXT, started_at TEXT, finished_at TEXT, error TEXT, delivery_outcome TEXT, scheduled_instant TEXT)""")
    for i, (status, day, error) in enumerate([("completed", "16", None), ("completed", "17", None), ("failed", "18", "Timed out")]):
        conn.execute("INSERT INTO executions (id, job_id, source, process_id, pid, status, claimed_at, started_at, finished_at, error)"
                     " VALUES (?, ?, 'b', 'p', 1, ?, ?, ?, ?, ?)",
                     (f"e{i}", BRIEFING, status, f"2026-10-{day}T07:00:00-04:00", f"2026-10-{day}T07:00:01-04:00",
                      f"2026-10-{day}T07:00:41-04:00", error))
    conn.commit()
    conn.close()
    (cron / "output" / BRIEFING / "2026-10-17_07-00-41.md").write_text("# Cron Job\n## Prompt\nlots\n## Response\n\nBriefing sent.")


@pytest.fixture
def world(tmp_path, monkeypatch):
    from gaca_api.config import settings
    settings.db_path = str(tmp_path / "t.db")
    monkeypatch.setattr(settings, "fcm_credentials_path", None)
    home = tmp_path / ".hermes"
    ledger(home)
    gateway = Gateway()
    adapter = HermesAdapter("http://engine:8642", "k" * 16, client=httpx.AsyncClient(transport=httpx.MockTransport(gateway)))
    box = {"provisioner": LocalHermes(home, "hermes")}
    monkeypatch.setattr(auth_service, "backend_for_crew", lambda user, member: adapter)
    monkeypatch.setattr("gaca_api.services.automations.provisioner_for", lambda user: box["provisioner"])
    app.dependency_overrides[get_current_user] = lambda: UserDoc(
        id="u1", email="u@example.com", display_name="T", agent_host="h", default_crew="default",
        crew=[CrewMember(name="default", port=1, api_key="k", color="blue")])
    with TestClient(app) as http:
        yield http, gateway, box
    app.dependency_overrides.clear()


def test_the_list_joins_jobs_with_their_recent_runs(world):
    http, gateway, _ = world
    body = http.get("/api/automations").json()
    assert body["unavailable"] == []
    by_id = {a["id"]: a for a in body["automations"]}
    assert ONCE not in by_id  # finished one-time jobs are hidden by default
    assert [a["id"] for a in body["automations"]] == [SCOUT, BRIEFING]  # soonest next run first
    briefing = by_id[BRIEFING]
    assert briefing["crew"] == "default" and briefing["status"] == "failed" and briefing["last_status"] == "failed"
    assert briefing["schedule"] == "Every day at 7:00 AM" and briefing["cron_expr"] == "0 7 * * *"
    assert [m["status"] for m in briefing["recent"]] == ["ok", "ok", "failed"]
    assert by_id[SCOUT]["status"] == "ok" and by_id[SCOUT]["recent"] == []
    assert ("GET", "/api/jobs", {"include_disabled": "true"}) in gateway.calls  # paused jobs are disabled ones


def test_include_done_and_crew_filter(world):
    http, _, _ = world
    ids = [a["id"] for a in http.get("/api/automations", params={"include_done": True}).json()["automations"]]
    assert ONCE in ids
    assert http.get("/api/automations", params={"crew": "default"}).status_code == 200
    assert http.get("/api/automations", params={"crew": "ghost"}).status_code == 404


def test_the_list_survives_an_unreadable_history_and_names_an_unreachable_engine(world):
    http, gateway, box = world

    class Broken(LocalHermes):
        async def read_cron_runs(self, *a, **k):
            from gaca_api.provisioning.base import ProvisioningError
            raise ProvisioningError("ssh down")

    box["provisioner"] = Broken(box["provisioner"]._root, "hermes")
    got = http.get("/api/automations").json()
    assert len(got["automations"]) == 2 and all(a["recent"] == [] for a in got["automations"])
    box["provisioner"] = None  # no direct access to the box at all
    assert len(http.get("/api/automations").json()["automations"]) == 2
    gateway.down = True
    assert http.get("/api/automations").json() == {"automations": [], "unavailable": ["default"]}


def test_runs_come_newest_first_with_what_each_said(world):
    http, _, _ = world
    runs = http.get(f"/api/crew/default/automations/{BRIEFING}/runs").json()
    assert [r["status"] for r in runs] == ["failed", "ok", "ok"]
    assert runs[0]["error"] == "Timed out" and runs[0]["summary"] is None and runs[0]["duration_seconds"] == 40.0
    assert runs[1]["summary"] == "Briefing sent."
    assert len(http.get(f"/api/crew/default/automations/{BRIEFING}/runs", params={"limit": 1}).json()) == 1
    assert http.get(f"/api/crew/default/automations/{BRIEFING}/runs", params={"limit": 0}).status_code == 422
    assert http.get("/api/crew/default/automations/dddddddddddd/runs").status_code == 404
    assert http.get("/api/crew/default/automations/bad%20id/runs").status_code == 404
    assert http.get(f"/api/crew/ghost/automations/{BRIEFING}/runs").status_code == 404


def test_runs_need_access_to_the_box(world):
    http, _, box = world
    box["provisioner"] = None
    assert http.get(f"/api/crew/default/automations/{BRIEFING}/runs").status_code == 502


def test_pause_resume_and_run_now(world):
    http, gateway, _ = world
    paused = http.post(f"/api/crew/default/automations/{SCOUT}/pause")
    assert paused.status_code == 200 and paused.json()["status"] == "paused" and paused.json()["next_run"] is None
    resumed = http.post(f"/api/crew/default/automations/{SCOUT}/resume").json()
    assert resumed["status"] == "ok" and resumed["next_run"] is not None
    ran = http.post(f"/api/crew/default/automations/{SCOUT}/run")
    assert ran.status_code == 202 and ran.json()["id"] == SCOUT
    assert ("POST", f"/api/jobs/{SCOUT}/run", {}) in gateway.calls


def test_errors_are_plain(world):
    http, gateway, _ = world
    assert http.post("/api/crew/default/automations/dddddddddddd/pause").status_code == 404
    gateway.refuse_run = True
    refused = http.post(f"/api/crew/default/automations/{ONCE}/run")
    assert refused.status_code == 422 and "terminal" in refused.json()["detail"]
    gateway.down = True
    assert http.post(f"/api/crew/default/automations/{SCOUT}/pause").status_code == 502
