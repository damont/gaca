"""GACA's Hermes skills (the repo's `skills/`): well-formed, generic, and the briefing helper works.

They are what teach a Hermes agent to put alerts through GACA, build the daily briefing in sections
and reach the API, so they must stay valid and must not carry anyone's personal details.
"""
import json
import os
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

SKILLS = Path(__file__).resolve().parents[2] / "skills"
EXPECTED = {"gaca", "gaca-alerts", "gaca-briefing", "gaca-artifacts", "gaca-heartbeat"}


def skill_dirs():
    return sorted(p for p in SKILLS.iterdir() if p.is_dir())


def frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text()
    assert text.startswith("---\n"), f"{path} has no frontmatter"
    block = text.split("\n---\n", 1)[0][4:]
    return {m.group(1): m.group(2) for m in re.finditer(r"^([a-z_]+):\s*(.*)$", block, re.M)}


def test_the_expected_skills_exist():
    assert {p.name for p in skill_dirs()} == EXPECTED


@pytest.mark.parametrize("skill", sorted(EXPECTED))
def test_each_skill_is_well_formed(skill):
    meta = frontmatter(SKILLS / skill / "SKILL.md")
    assert meta["name"] == skill
    assert 40 < len(meta["description"]) <= 1024
    assert "GACA_API_TOKEN" in (SKILLS / skill / "SKILL.md").read_text()
    assert "Wired in automatically by the GACA installer" in (SKILLS / skill / "SKILL.md").read_text()


def test_every_script_compiles():
    scripts = [p for p in SKILLS.rglob("*.py")]
    assert scripts
    for path in scripts:
        compile(path.read_text(), str(path), "exec")


def test_skills_refer_only_to_skills_that_exist():
    for skill in skill_dirs():
        for ref in re.findall(r"\bgaca-[a-z]+\b", (skill / "SKILL.md").read_text()):
            assert ref in EXPECTED or ref in ("gaca-api",), f"{skill.name} mentions unknown skill {ref}"


def test_the_core_skill_points_at_every_other_skill():
    text = (SKILLS / "gaca" / "SKILL.md").read_text()
    for other in EXPECTED - {"gaca"}:
        assert other in text


# Nothing that ties a skill to one person's setup: an email address, a LAN hostname
# or a home directory path.
PERSONAL = re.compile(r"[\w.+-]+@[\w-]+\.[a-z]{2,}|\.lan\b|/home/\w+|/Users/\w+", re.I)


def test_nothing_personal_is_baked_in():
    for path in SKILLS.rglob("*"):
        if path.is_file() and path.suffix in {".md", ".py", ".html", ".txt", ""}:
            hit = PERSONAL.search(path.read_text())
            assert hit is None, f"{path.relative_to(SKILLS)} mentions {hit.group(0)!r}"


def test_the_only_address_in_the_skills_is_the_local_api():
    for path in SKILLS.rglob("*"):
        if path.is_file():
            for url in re.findall(r"https?://[A-Za-z0-9._:-]+", path.read_text()):
                assert url in ("http://127.0.0.1:41700", "https://example.com") or url.startswith(("https://…", "http://localhost")), \
                    f"{path.relative_to(SKILLS)}: {url}"


def test_skills_tell_the_agent_never_to_use_a_public_address():
    assert "never" in (SKILLS / "gaca" / "SKILL.md").read_text().lower() and "127.0.0.1:41700" in (SKILLS / "gaca" / "SKILL.md").read_text()


# ── the briefing helper, against a fake GACA ─────────────────────────

class Recorder(BaseHTTPRequestHandler):
    log: list = []
    status = 200

    def _handle(self):
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length).decode() if length else ""
        Recorder.log.append((self.command, self.path, self.headers.get("authorization"), body))
        payload = {"id": "alert1", "items": json.loads(body).get("items", []) if body.startswith("{") else [],
                   "sections": [{"key": "weather", "title": "Weather", "items": [{"title": "x"}]}], "summary": None}
        self.send_response(Recorder.status)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())

    do_GET = do_PUT = do_POST = do_DELETE = _handle

    def log_message(self, *args):
        pass


@pytest.fixture
def gaca():
    Recorder.log, Recorder.status = [], 200
    server = HTTPServer(("127.0.0.1", 0), Recorder)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def run(*args, stdin="", env=None, check=True):
        full_env = {"PATH": os.environ["PATH"], "GACA_BASE_URL": f"http://127.0.0.1:{server.server_port}",
                    "GACA_API_TOKEN": "tok-123", "HOME": "/nonexistent", **(env or {})}
        result = subprocess.run([sys.executable, str(SKILLS / "gaca-briefing" / "scripts" / "briefing.py"), *args],
                                input=stdin, capture_output=True, text=True, env=full_env, timeout=60)
        if check:
            assert result.returncode == 0, result.stderr
        return result

    yield run
    server.shutdown()


SECTION = {"title": "Weather", "kind": "weather", "headline": "72° and sunny", "display_order": 20,
           "items": [{"title": "High 72°"}]}


def test_post_puts_the_section_with_the_token(gaca, tmp_path):
    f = tmp_path / "s.json"
    f.write_text(json.dumps(SECTION))
    out = gaca("post", "--date", "2026-10-11", "--key", "weather", "--file", str(f))
    assert "weather: posted (1 items)" in out.stdout
    method, path, auth, body = Recorder.log[0]
    assert (method, path, auth) == ("PUT", "/api/briefings/2026-10-11/sections/weather", "Bearer tok-123")
    assert json.loads(body)["headline"] == "72° and sunny"


def test_post_reads_stdin_when_there_is_no_file(gaca):
    gaca("post", "--date", "2026-10-11", "--key", "news", stdin=json.dumps(SECTION))
    assert Recorder.log[0][:2] == ("PUT", "/api/briefings/2026-10-11/sections/news")


def test_an_empty_section_is_removed_not_posted_blank(gaca):
    out = gaca("post", "--date", "2026-10-11", "--key", "news", stdin=json.dumps({"title": "News", "items": []}))
    assert Recorder.log[0][:2] == ("DELETE", "/api/briefings/2026-10-11/sections/news") and "removed" in out.stdout


@pytest.mark.parametrize("key", ["Weather", "has space", "-x", "a/b", "x" * 41, "../up"])
def test_a_bad_key_never_reaches_the_server(gaca, key):
    bad = gaca("post", "--date", "2026-10-11", "--key", key, stdin=json.dumps(SECTION), check=False)
    assert bad.returncode != 0 and Recorder.log == []


def test_a_bad_kind_or_bad_json_never_reaches_the_server(gaca):
    assert gaca("post", "--date", "2026-10-11", "--key", "x", stdin=json.dumps({**SECTION, "kind": "astrology"}), check=False).returncode != 0
    assert gaca("post", "--date", "2026-10-11", "--key", "x", stdin="{nope", check=False).returncode != 0
    assert Recorder.log == []


def test_a_bad_date_is_refused(gaca):
    assert gaca("summary", "--date", "tomorrow", "--text", "hi", check=False).returncode != 0 and Recorder.log == []


def test_summary_is_put_for_the_day(gaca):
    gaca("summary", "--date", "2026-10-11", "--text", "Busy day; rain after 3.")
    method, path, _, body = Recorder.log[0]
    assert (method, path, json.loads(body)) == ("PUT", "/api/briefings/2026-10-11", {"summary": "Busy day; rain after 3."})


def test_finish_sends_one_deduped_ready_alert(gaca):
    gaca("finish", "--date", "2026-10-11")
    get, post = Recorder.log
    assert get[:2] == ("GET", "/api/briefings?date=2026-10-11")
    alert = json.loads(post[3])
    assert post[:2] == ("POST", "/api/alerts") and alert["kind"] == "briefing.posted" and alert["severity"] == "info"
    assert alert["dedupe_key"] == "briefing:posted:2026-10-11" and alert["crew"] == "default"


def test_finish_with_problems_is_a_warning(gaca):
    gaca("finish", "--date", "2026-10-11", "--problem", "news: web search failed")
    alert = json.loads(Recorder.log[1][3])
    assert alert["severity"] == "warn" and "news: web search failed" in alert["body"]


def test_the_crew_is_the_hermes_profile_it_runs_as(gaca):
    gaca("finish", "--date", "2026-10-11", env={"HERMES_PROFILE": "research"})
    assert json.loads(Recorder.log[1][3])["crew"] == "research"


def test_a_missing_token_stops_before_any_request(gaca):
    r = gaca("show", "--date", "2026-10-11", env={"GACA_API_TOKEN": ""}, check=False)
    assert r.returncode != 0 and "GACA_API_TOKEN is not configured" in r.stderr and Recorder.log == []


def test_a_rejected_token_is_reported_not_retried(gaca):
    Recorder.status = 401
    r = gaca("show", "--date", "2026-10-11", check=False)
    assert r.returncode != 0 and "HTTP 401" in r.stderr and len(Recorder.log) == 1


def test_today_uses_the_configured_zone(gaca):
    r = gaca("today", env={"GACA_TIMEZONE": "Pacific/Kiritimati"})
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", r.stdout.strip())
    bad = gaca("today", env={"GACA_TIMEZONE": "Nope/Zone"}, check=False)
    assert bad.returncode != 0 and "not a valid time zone" in bad.stderr
