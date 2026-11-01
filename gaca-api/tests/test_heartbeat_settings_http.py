"""The heartbeat's settings: the app changes them, the agent can only read them.

Run against the real app and real auth, because the property that matters is enforcement: a model
holding an agent token must not be able to raise its own daily alert cap."""
from tests.test_api_tokens_http import bearer, http, mint  # noqa: F401  (fixture + helpers)

URL = "/api/heartbeat/settings"


def test_a_new_account_gets_the_defaults(http):
    body = http.get(URL, headers=http.session).json()
    assert body == {"enabled": True, "paused_until": None, "timezone": None, "quiet_start_hour": 22,
                    "quiet_end_hour": 7, "max_alerts_per_day": 3, "min_minutes_between_alerts": 60,
                    "max_runs_per_day": 30}


def test_a_change_is_saved_and_the_rest_is_left_alone(http):
    r = http.put(URL, headers=http.session, json={"max_alerts_per_day": 1, "quiet_start_hour": 21})
    assert r.status_code == 200 and r.json()["max_alerts_per_day"] == 1
    again = http.get(URL, headers=http.session).json()
    assert (again["max_alerts_per_day"], again["quiet_start_hour"], again["quiet_end_hour"]) == (1, 21, 7)
    http.put(URL, headers=http.session, json={"enabled": False})
    assert http.get(URL, headers=http.session).json()["max_alerts_per_day"] == 1  # an earlier change survives a later one


def test_a_pause_and_a_time_zone_can_be_set_and_then_cleared_with_null(http):
    http.put(URL, headers=http.session, json={"paused_until": "2026-11-02T08:00:00Z", "timezone": "America/Chicago"})
    body = http.get(URL, headers=http.session).json()
    assert body["timezone"] == "America/Chicago" and body["paused_until"].startswith("2026-11-02T08:00")
    http.put(URL, headers=http.session, json={"paused_until": None, "timezone": None})
    body = http.get(URL, headers=http.session).json()
    assert body["paused_until"] is None and body["timezone"] is None


def test_nonsense_is_a_422_and_stores_nothing(http):
    for bad in ({"quiet_start_hour": 24}, {"quiet_end_hour": -1}, {"max_alerts_per_day": 999},
                {"timezone": "Mars/Olympus"}, {"enabled": "maybe"}, {"max_runs_per_day": "lots"}):
        assert http.put(URL, headers=http.session, json=bad).status_code == 422, bad
    assert http.get(URL, headers=http.session).json()["max_alerts_per_day"] == 3


def test_an_agent_token_can_read_the_settings_but_never_change_them(http):
    agent = bearer(mint(http, "hermes-x", ["heartbeat"])["token"])
    assert http.get(URL, headers=agent).status_code == 200
    for body in ({"max_alerts_per_day": 20}, {"enabled": True}, {}):
        assert http.put(URL, headers=agent, json=body).status_code == 403
    assert http.get(URL, headers=http.session).json()["max_alerts_per_day"] == 3


def test_a_token_without_the_scope_cannot_even_read_them(http):
    other = bearer(mint(http, "alerts-only", ["alerts"])["token"])
    assert http.get(URL, headers=other).status_code == 403


def test_the_connect_command_mints_tokens_that_can_read_them(http):
    from gaca_api.utils import tokens as token_utils
    assert "heartbeat" in token_utils.SCOPES
