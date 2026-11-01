"""The gaca-heartbeat helper (skills/gaca-heartbeat/scripts/heartbeat.py), run for real against a fake GACA.

The helper is what keeps the heartbeat from burning the user's plan or nagging them, so its limits
are tested as behaviour: quiet hours, the daily run budget, the alert cap and gap, "the user already
dismissed this", and the pause after a rate-limit.
"""
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2] / "skills" / "gaca-heartbeat" / "scripts" / "heartbeat.py"


class Fake(BaseHTTPRequestHandler):
    alerts: list = []
    posted: list = []
    settings = None        # the app's heartbeat settings; None = this GACA has no such endpoint (404)
    settings_status = 200  # or 403: a token without the scope

    def _send(self, payload):
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())

    def do_GET(self):
        if self.path == "/api/heartbeat/settings":
            if Fake.settings is None or Fake.settings_status != 200:
                self.send_response(Fake.settings_status if Fake.settings_status != 200 else 404)
                self.end_headers()
                return
            return self._send(Fake.settings)
        self._send({"alerts": Fake.alerts})

    def do_POST(self):
        length = int(self.headers.get("content-length") or 0)
        Fake.posted.append((self.path, self.headers.get("authorization"), json.loads(self.rfile.read(length))))
        self._send({"id": "new-alert"})

    def log_message(self, *args):
        pass


@pytest.fixture
def beat(tmp_path):
    Fake.alerts, Fake.posted, Fake.settings, Fake.settings_status = [], [], None, 200
    server = HTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def run(*args, at="2026-10-31T12:00:00+00:00", stdin="", check=True):
        env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "HERMES_HOME": str(tmp_path / "hermes"),
               "GACA_BASE_URL": f"http://127.0.0.1:{server.server_port}", "GACA_API_TOKEN": "tok-123",
               "GACA_HEARTBEAT_NOW": at}
        result = subprocess.run([sys.executable, str(HELPER), *args], input=stdin, capture_output=True,
                                text=True, env=env, timeout=60)
        if check:
            assert result.returncode == 0, result.stderr
        return result

    yield run
    server.shutdown()


def gate(beat, **kw):
    return json.loads(beat("gate", **kw).stdout)


def stored(kind="reply.needed", status="new", key="k1", created="2026-10-30T09:00:00+00:00"):
    return {"source": "heartbeat", "kind": "heartbeat." + kind, "title": "t", "effective_status": status,
            "dedupe_key": key, "created_at": created}


ALERT = json.dumps({"kind": "reply.needed", "title": "Landlord wants Thursday", "dedupe_key": "mail:1:reply"})


def test_it_runs_in_the_daytime(beat):
    assert gate(beat)["run"] is True


@pytest.mark.parametrize("at", ["2026-10-31T23:30:00+00:00", "2026-10-31T03:00:00+00:00", "2026-10-31T06:59:00+00:00"])
def test_it_stays_quiet_at_night(beat, at):
    verdict = gate(beat, at=at)
    assert verdict["run"] is False and "quiet hours" in verdict["reason"]


def test_the_daily_run_budget_stops_it(beat):
    beat("config", "--set", "max_runs_per_day=2")
    assert [gate(beat)["run"] for _ in range(3)] == [True, True, False]


def test_the_budget_starts_over_the_next_day(beat):
    beat("config", "--set", "max_runs_per_day=1")
    assert gate(beat)["run"] is True and gate(beat)["run"] is False
    assert gate(beat, at="2026-11-01T12:00:00+00:00")["run"] is True


def test_a_rate_limit_pauses_it_until_the_pause_is_over(beat):
    beat("done", "--outcome", "rate_limited")
    assert gate(beat, at="2026-10-31T13:00:00+00:00")["run"] is False
    assert gate(beat, at="2026-10-31T16:00:00+00:00")["run"] is True


def test_the_pause_survives_midnight(beat):
    beat("done", "--outcome", "rate_limited", at="2026-10-31T21:30:00+00:00")
    verdict = gate(beat, at="2026-11-01T00:30:00+00:00")  # still paused, and also quiet hours
    assert verdict["run"] is False


def test_an_alert_is_posted_with_the_token_source_and_prefixed_kind(beat):
    beat("alert", stdin=ALERT)
    path, auth, body = Fake.posted[0]
    assert (path, auth) == ("/api/alerts", "Bearer tok-123")
    assert body["source"] == "heartbeat" and body["kind"] == "heartbeat.reply.needed" and body["crew"] == "default"


def test_the_daily_alert_cap_holds_back_the_next_one(beat):
    beat("config", "--set", "max_alerts_per_day=1", "--set", "min_minutes_between_alerts=0")
    beat("alert", stdin=ALERT)
    second = beat("alert", stdin=json.dumps({"kind": "x", "title": "other", "dedupe_key": "other"}), check=False)
    assert second.returncode == 3 and "HOLD" in second.stderr and len(Fake.posted) == 1


def test_alerts_must_be_spaced_out(beat):
    beat("alert", stdin=ALERT)
    soon = beat("alert", at="2026-10-31T12:20:00+00:00",
                stdin=json.dumps({"kind": "x", "title": "other", "dedupe_key": "other"}), check=False)
    assert soon.returncode == 3 and "minutes ago" in soon.stderr
    later = beat("alert", at="2026-10-31T13:10:00+00:00",
                 stdin=json.dumps({"kind": "x", "title": "other", "dedupe_key": "other"}))
    assert later.returncode == 0


def test_urgent_goes_past_the_cap_and_the_gap(beat):
    beat("alert", stdin=ALERT)
    urgent = beat("alert", stdin=json.dumps({"kind": "fire", "title": "now", "severity": "urgent", "dedupe_key": "f"}))
    assert urgent.returncode == 0 and len(Fake.posted) == 2


def test_something_the_user_already_dismissed_is_not_raised_again(beat):
    Fake.alerts = [stored(status="dismissed", key="mail:1:reply")]
    held = beat("alert", stdin=ALERT, check=False)
    assert held.returncode == 3 and "dismissed" in held.stderr and Fake.posted == []


def test_an_open_alert_can_be_updated_without_spending_the_budget(beat):
    Fake.alerts = [stored(status="new", key="mail:1:reply")]
    beat("config", "--set", "max_alerts_per_day=1")
    beat("alert", stdin=ALERT)
    beat("alert", stdin=ALERT)
    assert len(Fake.posted) == 2  # both went through: a re-post of an open alert is free


def test_a_bad_severity_or_missing_kind_never_reaches_the_server(beat):
    assert beat("alert", stdin=json.dumps({"kind": "x", "title": "t", "severity": "loud"}), check=False).returncode != 0
    assert beat("alert", stdin=json.dumps({"title": "t"}), check=False).returncode != 0
    assert beat("alert", stdin="{nope", check=False).returncode != 0
    assert Fake.posted == []


def test_context_says_what_the_user_did_with_earlier_alerts(beat):
    Fake.alerts = [stored(status="dismissed", kind="newsletter", key="a"), stored(status="dismissed", kind="newsletter", key="b"),
                   stored(status="acted", kind="reply.needed", key="c"),
                   {**stored(key="foreign-key"), "source": "someone-else"},                       # not the heartbeat's
                   stored(key="stale-key", created="2026-01-01T00:00:00+00:00")]              # outside the window
    out = beat("context").stdout
    assert "heartbeat.newsletter: 2 dismissed" in out and "heartbeat.reply.needed: 1 acted" in out
    assert "foreign-key" not in out and "stale-key" not in out
    assert "3 of 3 left" in out


def test_context_with_no_history_says_so(beat):
    assert "no record of what this user cares about" in beat("context").stdout


def test_config_changes_stick_and_bad_ones_are_refused(beat):
    assert json.loads(beat("config", "--set", "max_alerts_per_day=5").stdout)["max_alerts_per_day"] == 5
    assert json.loads(beat("config").stdout)["max_alerts_per_day"] == 5
    assert beat("config", "--set", "nonsense=1", check=False).returncode != 0
    assert beat("config", "--set", "quiet_start_hour=30", check=False).returncode != 0
    assert beat("config", "--set", "max_runs_per_day=lots", check=False).returncode != 0


# ── the limits come from the app ─────────────────────────────────────

def app(**changes):
    Fake.settings = {"enabled": True, "paused_until": None, "timezone": None, "quiet_start_hour": 22, "quiet_end_hour": 7,
                     "max_alerts_per_day": 3, "min_minutes_between_alerts": 60, "max_runs_per_day": 30, **changes}


def test_the_apps_quiet_hours_replace_the_local_ones(beat):
    app(quiet_start_hour=11, quiet_end_hour=13)  # the user wants noon to be quiet
    verdict = gate(beat, at="2026-10-31T12:00:00+00:00")
    assert verdict["run"] is False and "quiet hours (11:00-13:00)" in verdict["reason"] and verdict["settings_from"] == "app"


def test_without_app_settings_it_says_the_limits_are_local(beat):
    assert gate(beat)["settings_from"] == "local"


@pytest.mark.parametrize("status", [403, 404])
def test_a_gaca_that_cannot_supply_settings_means_the_local_limits(beat, status):
    Fake.settings, Fake.settings_status = {"max_runs_per_day": 0}, status  # 403 = a token without the scope
    beat("config", "--set", "max_runs_per_day=1")
    verdict = gate(beat)
    assert verdict["run"] is True and verdict["settings_from"] == "local"
    assert gate(beat)["run"] is False  # the local budget of 1 held


def test_when_gaca_does_not_answer_at_all_it_still_runs_on_the_local_limits(beat, tmp_path):
    import subprocess as sp
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "HERMES_HOME": str(tmp_path / "h"),
           "GACA_BASE_URL": "http://127.0.0.1:9", "GACA_API_TOKEN": "tok", "GACA_HEARTBEAT_NOW": "2026-10-31T12:00:00+00:00"}
    out = sp.run([sys.executable, str(HELPER), "gate"], capture_output=True, text=True, env=env, timeout=60)
    assert out.returncode == 0 and json.loads(out.stdout)["run"] is True


def test_the_switch_in_the_app_stops_it(beat):
    app(enabled=False)
    verdict = gate(beat)
    assert verdict["run"] is False and "switched off in the app" in verdict["reason"]


def test_a_pause_in_the_app_holds_until_it_ends(beat):
    app(paused_until="2026-10-31T15:00:00Z")
    assert gate(beat, at="2026-10-31T13:00:00+00:00")["run"] is False
    assert gate(beat, at="2026-10-31T16:00:00+00:00")["run"] is True


def test_the_apps_alert_cap_wins_over_a_looser_local_one(beat):
    beat("config", "--set", "max_alerts_per_day=10", "--set", "min_minutes_between_alerts=0")
    app(max_alerts_per_day=1, min_minutes_between_alerts=0)
    beat("alert", stdin=ALERT)
    held = beat("alert", stdin=json.dumps({"kind": "x", "title": "other", "dedupe_key": "other"}), check=False)
    assert held.returncode == 3 and len(Fake.posted) == 1


def test_a_zero_cap_in_the_app_silences_it_except_for_urgent(beat):
    app(max_alerts_per_day=0)
    assert beat("alert", stdin=ALERT, check=False).returncode == 3
    assert beat("alert", stdin=json.dumps({"kind": "fire", "title": "now", "severity": "urgent", "dedupe_key": "f"})).returncode == 0
