"""The gaca-reminders helper, and the heartbeat's first step that fires due reminders, run for real
against a fake GACA. What matters here is what the agent cannot get wrong: the time sent is the right
UTC instant for the user's zone, "explicit" is only set when asked, nothing is invented, the token is
never printed, and a GACA that cannot do reminders never breaks the heartbeat."""
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

SKILLS = Path(__file__).resolve().parents[2] / "skills"
HELPER = SKILLS / "gaca-reminders" / "scripts" / "reminders.py"
HEARTBEAT = SKILLS / "gaca-heartbeat" / "scripts" / "heartbeat.py"
TOKEN = "tok-secret-123"


def reminder(**kw):
    return {"id": "r1", "text": "Call the dentist", "kind": "timed", "state": "pending", "due_at": "2026-11-23T16:00:00Z",
            "explicit_time": True, "duplicate": False, **kw}


class Fake(BaseHTTPRequestHandler):
    seen: list = []
    replies: dict = {}  # (method, path) -> (status, body); anything else is a 404

    def _handle(self, method):
        length = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(length) or b"null") if length else None
        Fake.seen.append((method, self.path, self.headers.get("authorization"), body))
        status, payload = Fake.replies.get((method, self.path.split("?")[0]), (404, {"detail": "no"}))
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())

    def do_GET(self): self._handle("GET")      # noqa: E704
    def do_POST(self): self._handle("POST")    # noqa: E704
    def do_PATCH(self): self._handle("PATCH")  # noqa: E704
    def log_message(self, *args): pass         # noqa: E704


@pytest.fixture
def gaca(tmp_path):
    Fake.seen, Fake.replies = [], {("GET", "/api/heartbeat/settings"): (200, {"timezone": "America/Chicago"})}
    server = HTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def run(script, *args, env=None, stdin="", port=None):
        full = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "HERMES_HOME": str(tmp_path / "hermes"),
                "GACA_BASE_URL": f"http://127.0.0.1:{port or server.server_port}", "GACA_API_TOKEN": TOKEN,
                "GACA_REMINDERS_NOW": "2026-11-22T15:00:00+00:00", "GACA_HEARTBEAT_NOW": "2026-11-22T15:00:00+00:00",
                "HERMES_PROFILE": "trading", **(env or {})}
        result = subprocess.run([sys.executable, str(script), *args], input=stdin, capture_output=True, text=True, env=full, timeout=60)
        assert TOKEN not in result.stdout + result.stderr  # never printed
        return result

    run.port = server.server_port
    yield run
    server.shutdown()


def posted():
    return [(m, p, b) for m, p, _a, b in Fake.seen if m != "GET"]


# ── gaca-reminders ───────────────────────────────────────────────────

def test_now_gives_the_date_weekday_and_the_users_zone(gaca):
    out = json.loads(gaca(HELPER, "now").stdout)
    assert out["weekday"] == "Sunday" and out["zone"] == "America/Chicago" and out["now"].startswith("2026-11-22T09:00")


def test_a_time_with_no_offset_means_the_users_own_zone(gaca):
    Fake.replies[("POST", "/api/reminders")] = (201, reminder())
    r = gaca(HELPER, "create", "--text", "Call the dentist", "--at", "2026-11-23T10:00", "--explicit", "--kind", "chat", "--session", "s9")
    assert r.returncode == 0, r.stderr
    [(_, _, body)] = posted()
    assert body == {"text": "Call the dentist", "due_at": "2026-11-23T16:00:00Z", "explicit_time": True,
                    "source": {"kind": "chat", "crew": "trading", "session_id": "s9"}}  # 10:00 CST is 16:00 UTC
    out = json.loads(r.stdout)
    assert out["confirm"] == "Alert set for Monday Nov 23 at 10:00 AM (delivered within half an hour of that time)."
    assert Fake.seen[-1][2] == f"Bearer {TOKEN}"


def test_an_explicit_offset_is_respected_and_explicit_is_off_unless_asked(gaca):
    Fake.replies[("POST", "/api/reminders")] = (201, reminder(explicit_time=False))
    gaca(HELPER, "create", "--text", "Tidy up", "--at", "2026-11-23T09:00:00-08:00")
    [(_, _, body)] = posted()
    assert body["due_at"] == "2026-11-23T17:00:00Z" and body["explicit_time"] is False and body["source"]["kind"] == "agent"


def test_no_time_makes_a_later_note_with_no_due_at(gaca):
    Fake.replies[("POST", "/api/reminders")] = (201, reminder(kind="note", due_at=None, explicit_time=False))
    r = gaca(HELPER, "create", "--text", "Look into a standing desk")
    [(_, _, body)] = posted()
    assert "due_at" not in body and "explicit_time" not in body
    assert "won't alert you" in json.loads(r.stdout)["confirm"]


def test_a_duplicate_is_reported_as_one(gaca):
    Fake.replies[("POST", "/api/reminders")] = (200, reminder(duplicate=True))
    out = json.loads(gaca(HELPER, "create", "--text", "Call the dentist", "--at", "2026-11-23T10:00").stdout)
    assert out["duplicate"] is True and out["confirm"].startswith("You already had that one")


@pytest.mark.parametrize("args", [["--text", "x", "--explicit"], ["--text", "x", "--at", "tomorrow at ten"], ["--text", "x", "--at", "2026-11-23T10:00", "--expires", "soon"]])
def test_nonsense_is_refused_before_anything_is_sent(gaca, args):
    r = gaca(HELPER, "create", *args)
    assert r.returncode == 1 and posted() == []


def test_the_zone_falls_back_to_the_environment_when_the_app_has_no_setting(gaca):
    Fake.replies.pop(("GET", "/api/heartbeat/settings"))
    Fake.replies[("POST", "/api/reminders")] = (201, reminder())
    gaca(HELPER, "create", "--text", "x", "--at", "2026-11-23T10:00", env={"GACA_TIMEZONE": "America/New_York"})
    assert posted()[0][2]["due_at"] == "2026-11-23T15:00:00Z"


def test_list_reschedule_cancel_and_done_use_the_right_calls(gaca):
    Fake.replies[("GET", "/api/reminders")] = (200, {"reminders": [reminder()]})
    Fake.replies[("PATCH", "/api/reminders/r1")] = (200, reminder())
    listed = json.loads(gaca(HELPER, "list", "--state", "open").stdout)
    assert listed[0]["id"] == "r1" and ("GET", "/api/reminders?state=open") in [(m, p) for m, p, _a, _b in Fake.seen]
    gaca(HELPER, "reschedule", "r1", "--at", "2026-11-23T11:00", "--explicit")
    gaca(HELPER, "cancel", "r1")
    gaca(HELPER, "done", "r1")
    assert [b for _, _, b in posted()] == [{"due_at": "2026-11-23T17:00:00Z", "explicit_time": True}, {"state": "cancelled"}, {"state": "done"}]


def test_a_token_without_the_scope_is_reported_not_worked_around(gaca):
    Fake.replies[("POST", "/api/reminders")] = (403, {"detail": "token may not call this endpoint"})
    r = gaca(HELPER, "create", "--text", "x")
    assert r.returncode == 2 and "403" in r.stderr and len(posted()) == 1


# ── the heartbeat's first step ───────────────────────────────────────

def test_the_heartbeat_fires_due_reminders_and_says_how_many(gaca):
    Fake.replies[("POST", "/api/reminders/fire-due")] = (200, {"fired": [reminder(), reminder(id="r2", text="Pay rent")], "held": 1, "expired": 0})
    r = gaca(HEARTBEAT, "reminders")
    assert r.returncode == 0 and json.loads(r.stdout) == {"fired": 2, "texts": ["Call the dentist", "Pay rent"], "held": 1, "expired": 0}
    assert posted()[0][:2] == ("POST", "/api/reminders/fire-due") and Fake.seen[-1][2] == f"Bearer {TOKEN}"


def test_it_runs_in_quiet_hours_when_the_gate_says_no(gaca):
    Fake.replies[("POST", "/api/reminders/fire-due")] = (200, {"fired": [], "held": 0, "expired": 0})
    night = {"GACA_HEARTBEAT_NOW": "2026-11-22T03:00:00+00:00"}
    assert json.loads(gaca(HEARTBEAT, "gate", env=night).stdout)["run"] is False
    assert gaca(HEARTBEAT, "reminders", env=night).returncode == 0
    assert posted()[0][:2] == ("POST", "/api/reminders/fire-due")


def test_it_does_not_count_as_a_heartbeat_run(gaca):
    Fake.replies[("POST", "/api/reminders/fire-due")] = (200, {"fired": [], "held": 0, "expired": 0})
    for _ in range(3):
        gaca(HEARTBEAT, "reminders")
    assert json.loads(gaca(HEARTBEAT, "gate").stdout)["runs_today"] == 1  # only the gate's own run


@pytest.mark.parametrize("status,words", [(403, "lacks the reminders scope"), (404, "no reminders yet"), (500, "answered 500")])
def test_an_old_or_unwilling_gaca_never_breaks_the_heartbeat(gaca, status, words):
    Fake.replies[("POST", "/api/reminders/fire-due")] = (status, {"detail": "x"})
    r = gaca(HEARTBEAT, "reminders")
    assert r.returncode == 0 and "skipped" in r.stdout and words in r.stdout


def test_no_answer_or_no_token_is_skipped_quietly(gaca):
    assert "skipped" in gaca(HEARTBEAT, "reminders", port=1).stdout and gaca(HEARTBEAT, "reminders", port=1).returncode == 0
    r = gaca(HEARTBEAT, "reminders", env={"GACA_API_TOKEN": ""})
    assert r.returncode == 0 and "skipped" in r.stdout and posted() == []
