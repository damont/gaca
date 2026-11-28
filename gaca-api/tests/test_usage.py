"""Token usage (GET /api/usage) read off a fake Hermes home, and the review's rules."""
import json
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from gaca_api.main import app
from gaca_api.models.user import CrewMember, UserDoc
from gaca_api.provisioning import usage_read
from gaca_api.provisioning.local import LocalHermes
from gaca_api.schemas.usage import UsageLine
from gaca_api.services import usage as usage_service
from gaca_api.utils.auth import get_current_user

HEARTBEAT, MOVES, BRIEF = "c85917c3a264", "c989484fab1b", "6ff4eeebd3f1"


def state_db(directory, sessions):
    """A state.db with just the columns usage_read uses. `sessions`: (id, source, title, started, calls,
    input, cache_read, output, last reply)."""
    directory.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(directory / "state.db")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, title TEXT, model TEXT, started_at REAL,
        api_call_count INTEGER, input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
        cache_write_tokens INTEGER, reasoning_tokens INTEGER)""")
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT)")
    for sid, source, title, started, calls, inp, cached, out, reply in sessions:
        conn.execute("INSERT INTO sessions VALUES (?, ?, ?, 'gpt-x', ?, ?, ?, ?, ?, 0, 0)",
                     (sid, source, title, started, calls, inp, out, cached))
        conn.execute("INSERT INTO messages (session_id, role, content) VALUES (?, 'user', 'go')", (sid,))
        conn.execute("INSERT INTO messages (session_id, role, content) VALUES (?, 'assistant', ?)", (sid, reply))
        conn.execute("INSERT INTO messages (session_id, role, content) VALUES (?, 'assistant', '')", (sid,))  # a tool-call row
    conn.commit()
    conn.close()


def jobs_json(directory, jobs):
    (directory / "cron").mkdir(parents=True, exist_ok=True)
    (directory / "cron" / "jobs.json").write_text(json.dumps({"jobs": jobs}))


def cron(job_id, name, n, when):
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(when))
    return f"cron_{job_id}_{stamp}", "cron", f"{name} · Nov {n:02d} 10:00"


@pytest.fixture
def home(tmp_path):
    root = tmp_path / ".hermes"
    now = time.time()
    rows = []
    # The heartbeat: 40 runs this week, 30 silent, 20k tokens each.
    for i in range(40):
        sid, source, title = cron(HEARTBEAT, "gaca-heartbeat", i, now - 3600 * (i + 1))
        rows.append((sid, source, title, now - 3600 * (i + 1), 4, 4000, 16000, 100, "[SILENT]" if i < 30 else "Looked at: mail"))
    # A daily briefing: 7 runs, not silent.
    for i in range(7):
        sid, source, title = cron(BRIEF, "daily-briefing", i, now - 86400 * i - 60)
        rows.append((sid, source, title, now - 86400 * i - 60, 10, 5000, 30000, 2000, "Briefing posted"))
    # Chats from the app, and one heartbeat run from the week before.
    rows.append(("s-chat-1", "api_server", "Help me", now - 7200, 3, 2000, 8000, 500, "Sure"))
    sid, source, title = cron(HEARTBEAT, "gaca-heartbeat", 99, now - 8 * 86400)
    rows.append((sid, source, title, now - 8 * 86400, 4, 4000, 16000, 100, "[SILENT]"))
    state_db(root, rows)
    jobs_json(root, [
        {"id": HEARTBEAT, "name": "gaca-heartbeat", "schedule": {"expr": "*/30 * * * *", "display": "*/30 * * * *"},
         "script": None, "skills": ["gaca-heartbeat"], "enabled": True},
        {"id": BRIEF, "name": "daily-briefing", "schedule_display": "0 6 * * *", "script": None, "enabled": True},
    ])
    # The trading profile: a price watcher that is nearly always silent.
    trading = root / "profiles" / "trading"
    state_db(trading, [(*cron(MOVES, "stock-notebook-price-moves", i, now - 1800 * (i + 1))[:3], now - 1800 * (i + 1),
                        6, 8000, 30000, 200, "[SILENT]" if i % 10 else "NVDA moved 4%") for i in range(60)])
    jobs_json(trading, [{"id": MOVES, "name": "stock-notebook-price-moves", "schedule_display": "*/30 7-18 * * 1-5",
                         "script": None, "enabled": True}])
    return root


def test_the_reader_groups_by_job_and_counts_silent_runs(home):
    now = time.time()
    data = usage_read.read(home, now - 7 * 86400, now)
    by_key = {g["key"]: g for g in data["groups"]}
    beat = by_key[f"job:{HEARTBEAT}"]
    assert (beat["runs"], beat["silent_runs"], beat["calls"]) == (40, 30, 160)
    assert beat["name"] == "gaca-heartbeat" and beat["input_tokens"] == 160_000 and beat["cache_read_tokens"] == 640_000
    assert by_key["chat:api_server"]["runs"] == 1 and by_key["chat:api_server"]["kind"] == "chat"
    assert {j["id"] for j in data["jobs"]} == {HEARTBEAT, BRIEF}
    assert next(j for j in data["jobs"] if j["id"] == HEARTBEAT)["schedule_display"] == "*/30 * * * *"


def test_a_missing_database_is_empty_not_an_error(tmp_path):
    assert usage_read.read(tmp_path, 0, time.time()) == {"groups": [], "jobs": []}


def test_the_reader_also_works_on_a_read_only_copy(home, tmp_path):
    """The service's sandbox mounts Hermes read-only: no -shm can be made there."""
    import shutil
    import stat

    copy = tmp_path / "ro"
    copy.mkdir()
    shutil.copy(home / "state.db", copy / "state.db")
    copy.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        data = usage_read.read(copy, 0, time.time() + 10)
    finally:
        copy.chmod(stat.S_IRWXU)
    assert any(g["key"] == f"job:{HEARTBEAT}" for g in data["groups"])


@pytest.fixture
def http(home, monkeypatch, tmp_path):
    from gaca_api.config import settings

    settings.db_path = str(tmp_path / "t.db")
    monkeypatch.setattr(settings, "agent_engine", "hermes")
    monkeypatch.setattr(settings, "fcm_credentials_path", None)
    monkeypatch.setattr(usage_service, "provisioner_for", lambda user: LocalHermes(home, "hermes"))
    app.dependency_overrides[get_current_user] = lambda: UserDoc(
        id="u1", email="u@example.com", display_name="T", agent_host="h", default_crew="default",
        crew=[CrewMember(name="default", port=1, api_key="k", color="blue"),
              CrewMember(name="trading", port=1, path="/p/trading", api_key="k2", color="green"),
              CrewMember(name="ghost", port=1, path="/p/ghost", api_key="k3", color="red")])
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


def test_usage_lists_every_crew_members_jobs_biggest_first_with_findings(http):
    body = http.get("/api/usage").json()
    assert body["days"] == 7 and body["unavailable"] == ["ghost"]
    names = [(line["crew"], line["name"]) for line in body["lines"]]
    assert names[0] == ("trading", "stock-notebook-price-moves")
    assert ("default", "Chats from the app") in names
    assert abs(sum(line["share"] for line in body["lines"]) - 1) < 0.01
    beat = next(line for line in body["lines"] if line["name"] == "gaca-heartbeat")
    assert beat["previous_total_tokens"] == 20_100 and beat["schedule"] == "*/30 * * * *" and beat["has_prerun"] is False
    rules = {(f["rule"], f["name"]) for f in body["findings"]}
    assert ("prerun", "stock-notebook-price-moves") in rules
    assert ("heartbeat_prerun", "gaca-heartbeat") in rules
    assert body["cost"]["usd"] > 0 and body["cost"]["usd_per_month"] > body["cost"]["usd"]


def test_usage_is_for_the_app_only_not_agent_tokens():
    from gaca_api.utils import tokens

    assert not any(tokens.allowed([scope], "GET", "/api/usage") for scope in tokens.SCOPES)


def line(name, *, runs=50, silent=40, total=1_000_000, share=0.3, calls=200, prerun=False, crew="default"):
    return UsageLine(crew=crew, kind="automation", automation_id="j" + name[:5], name=name, runs=runs, silent_runs=silent,
                     calls=calls, prompt_tokens=total - 1000, cached_tokens=total // 2, output_tokens=1000,
                     total_tokens=total, share=share, has_prerun=prerun)


def test_a_job_that_already_has_a_prerun_is_not_told_to_get_one():
    assert usage_service.findings([line("watcher", prerun=True, share=0.1)], 7) == []


def test_a_rarely_silent_but_huge_job_gets_a_review_not_a_prerun():
    [finding] = usage_service.findings([line("big", silent=5, share=0.5)], 7)
    assert finding.rule == "review"


def test_small_jobs_and_chats_get_no_findings():
    chat = line("Chats", share=0.6).model_copy(update={"kind": "chat"})
    assert usage_service.findings([line("tiny", share=0.01), chat], 7) == []


def test_a_huge_prompt_per_call_is_flagged_even_when_the_share_is_modest():
    [finding] = usage_service.findings([line("fat", silent=0, share=0.08, calls=10, total=600_000)], 7)
    assert finding.rule == "review" and "per model call" in finding.detail


def test_findings_are_capped_and_biggest_saving_first():
    lines = [line(f"job{i}", total=(i + 1) * 1_000_000, share=0.1) for i in range(6)]
    found = usage_service.findings(lines, 7)
    assert len(found) == usage_service.MAX_FINDINGS
    assert [f.name for f in found] == ["job5", "job4", "job3", "job2"]
