"""API tokens: what Hermes skills authenticate with. Scoped, revocable, never stored in the clear.

The security-critical part is enforcement, so most of this file is "what a token must NOT be
able to do" — against the real app, with the real auth dependency.
"""
import json
import sqlite3
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from gaca_api.config import settings
from gaca_api.main import app
from gaca_api.models.base import utcnow
from gaca_api.services import setup as setup_service
from gaca_api.utils import tokens as token_utils

CODE = "ABCD2345EFGH6789"


@pytest.fixture
def http(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "t.db"))
    monkeypatch.setattr(settings, "agent_engine", "hermes")
    with TestClient(app) as client:
        client.db = str(tmp_path / "t.db")
        con = sqlite3.connect(client.db)
        state = {"hash": setup_service._digest(CODE), "expires_at": (utcnow() + timedelta(hours=1)).isoformat(), "wrong": 0}
        con.execute("INSERT INTO server_meta (key, value) VALUES ('setup_code', ?)", (json.dumps(state),))
        con.commit()
        con.close()
        made = client.post("/api/setup/claim", json={"code": CODE, "name": "O", "email": "o@example.com", "password": "correct horse"})
        client.session = {"Authorization": "Bearer " + made.json()["access_token"]}
        yield client


def mint(http, name="hermes-test", scopes=("alerts", "lists", "briefings", "artifacts"), **extra):
    r = http.post("/api/tokens", headers=http.session, json={"name": name, "scopes": list(scopes), **extra})
    assert r.status_code == 201, r.text
    return r.json()


def bearer(secret):
    return {"Authorization": f"Bearer {secret}"}


def db_rows(http, sql, *args):
    con = sqlite3.connect(http.db)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


# ── minting and storage ──────────────────────────────────────────────

def test_minting_returns_the_secret_once_and_stores_only_a_hash(http):
    made = mint(http)
    assert made["token"].startswith(token_utils.PREFIX) and len(made["token"]) > 40
    listed = http.get("/api/tokens", headers=http.session).json()
    assert [t["id"] for t in listed] == [made["id"]]
    assert "token" not in listed[0] and "token_hash" not in listed[0]
    stored = db_rows(http, "SELECT token_hash FROM api_tokens")[0][0]
    assert stored == token_utils.digest(made["token"]) and made["token"] not in stored
    assert made["token"].encode() not in open(http.db, "rb").read()


def test_minting_validates_name_and_scopes(http):
    def post(**body):
        return http.post("/api/tokens", headers=http.session, json=body).status_code
    assert post(name="x", scopes=[]) == 422
    assert post(name="x", scopes=["alerts", "everything"]) == 422
    assert post(name="x", scopes=["crew"]) == 422
    assert post(name="", scopes=["alerts"]) == 422
    assert post(name="x" * 61, scopes=["alerts"]) == 422
    assert post(name="x", scopes=["alerts"], expires_in_days=0) == 422


def test_the_old_full_power_agent_token_endpoint_is_gone(http):
    assert http.post("/api/auth/agent-token", headers=http.session, json={}).status_code in (404, 405)
    assert "/api/auth/agent-token" not in app.openapi()["paths"]


# ── what a token CAN do ──────────────────────────────────────────────

def test_a_token_can_do_what_hermes_skills_need(http):
    h = bearer(mint(http)["token"])
    assert http.get("/api/auth/me", headers=h).json()["email"] == "o@example.com"
    made = http.post("/api/alerts", headers=h, json={"kind": "test", "title": "from hermes"})
    assert made.status_code in (200, 201), made.text
    alert_id = made.json()["id"]
    assert http.get("/api/alerts", headers=h).status_code == 200
    assert http.get(f"/api/alerts/{alert_id}", headers=h).status_code == 200
    assert http.get("/api/lists", headers=h).status_code == 200
    assert http.get("/api/briefings", params={"date": "2026-10-10"}, headers=h).status_code == 200
    assert http.get("/api/artifacts", headers=h).status_code == 200


# ── what a token must NOT be able to do ──────────────────────────────

@pytest.mark.parametrize("method,path", [
    ("POST", "/api/alerts/{id}/act"), ("POST", "/api/alerts/{id}/dismiss"), ("POST", "/api/alerts/{id}/snooze"),
    ("POST", "/api/alerts/{id}/delegate"), ("POST", "/api/alerts/{id}/take-to-chat"),
    ("GET", "/api/crew"), ("POST", "/api/crew"), ("POST", "/api/crew/discover"), ("GET", "/api/crew/default/profile"),
    ("GET", "/api/sessions"), ("POST", "/api/sessions"), ("GET", "/api/agent"), ("PUT", "/api/agent"),
    ("POST", "/api/push/register"), ("POST", "/api/push/unregister"),
    ("GET", "/api/tokens"), ("POST", "/api/tokens"), ("DELETE", "/api/tokens/anything"),
])
def test_a_token_is_refused_everywhere_its_scopes_do_not_reach(http, method, path):
    h = bearer(mint(http)["token"])
    alert = http.post("/api/alerts", headers=h, json={"kind": "t", "title": "x"}).json()["id"]
    r = http.request(method, path.replace("{id}", alert), headers=h, json={"name": "x", "scopes": ["alerts"]})
    assert r.status_code in (401, 403, 404, 405), f"{method} {path} -> {r.status_code}"
    if path.startswith(("/api/alerts/", "/api/crew", "/api/sessions", "/api/agent", "/api/push", "/api/tokens")):
        assert r.status_code == 403, f"{method} {path} -> {r.status_code}"


def test_an_api_token_cannot_mint_or_revoke_tokens(http):
    first = mint(http)
    h = bearer(first["token"])
    assert http.post("/api/tokens", headers=h, json={"name": "evil", "scopes": ["alerts"]}).status_code == 403
    assert http.delete(f"/api/tokens/{first['id']}", headers=h).status_code == 403
    assert len(http.get("/api/tokens", headers=http.session).json()) == 1  # nothing was minted


def test_scopes_are_independent(http):
    lists_only = bearer(mint(http, "a", ["lists"])["token"])
    alerts_only = bearer(mint(http, "b", ["alerts"])["token"])
    assert http.get("/api/lists", headers=lists_only).status_code == 200
    assert http.get("/api/alerts", headers=lists_only).status_code == 403
    assert http.get("/api/alerts", headers=alerts_only).status_code == 200
    assert http.get("/api/lists", headers=alerts_only).status_code == 403
    assert http.get("/api/auth/me", headers=lists_only).status_code == 200  # "is my token good" always works


@pytest.mark.parametrize("path", ["/api/lists/../crew", "/api/lists/./x", "/api//alerts", "/api/alerts/../crew"])
def test_dot_segments_cannot_walk_a_token_out_of_its_scope(path):
    assert token_utils.allowed(["alerts", "lists"], "GET", path) is False


# ── lifecycle ────────────────────────────────────────────────────────

def test_revoking_cuts_the_token_off_immediately_and_is_idempotent(http):
    made = mint(http)
    h = bearer(made["token"])
    assert http.get("/api/lists", headers=h).status_code == 200
    assert http.delete(f"/api/tokens/{made['id']}", headers=http.session).status_code == 204
    assert http.get("/api/lists", headers=h).status_code == 401
    assert http.delete(f"/api/tokens/{made['id']}", headers=http.session).status_code == 204
    assert http.delete("/api/tokens/nope", headers=http.session).status_code == 404
    listed = http.get("/api/tokens", headers=http.session).json()
    assert listed[0]["revoked_at"] is not None


def test_an_expired_token_stops_working(http):
    made = mint(http, expires_in_days=1)
    h = bearer(made["token"])
    assert http.get("/api/lists", headers=h).status_code == 200
    con = sqlite3.connect(http.db)
    con.execute("UPDATE api_tokens SET expires_at = ? WHERE id = ?", ((utcnow() - timedelta(seconds=1)).isoformat(), made["id"]))
    con.commit()
    con.close()
    assert http.get("/api/lists", headers=h).status_code == 401


def test_a_token_does_not_expire_unless_asked(http):
    assert mint(http)["expires_at"] is None


def test_unknown_or_tampered_tokens_are_401(http):
    secret = mint(http)["token"]
    for bad in (secret[:-1] + ("A" if secret[-1] != "A" else "B"), token_utils.PREFIX + "x" * 43, token_utils.PREFIX, secret.upper()):
        assert http.get("/api/lists", headers=bearer(bad)).status_code == 401


def test_a_disabled_owner_loses_every_token(http):
    secret = mint(http)["token"]
    con = sqlite3.connect(http.db)
    doc = json.loads(con.execute("SELECT doc FROM users").fetchone()[0])
    doc["is_active"] = False
    con.execute("UPDATE users SET doc = ?", (json.dumps(doc),))
    con.commit()
    con.close()
    assert http.get("/api/lists", headers=bearer(secret)).status_code == 401


def test_using_a_token_records_when(http):
    made = mint(http)
    assert http.get("/api/tokens", headers=http.session).json()[0]["last_used_at"] is None
    http.get("/api/lists", headers=bearer(made["token"]))
    assert http.get("/api/tokens", headers=http.session).json()[0]["last_used_at"] is not None


def test_sign_in_sessions_still_work_alongside_tokens(http):
    assert http.get("/api/auth/me", headers=http.session).status_code == 200
    assert http.get("/api/crew", headers=http.session).status_code == 200


def test_token_management_stays_session_only_even_if_a_scope_were_widened(http, monkeypatch):
    """Defence in depth: the scope rules don't mention /api/tokens, and the endpoints ALSO demand a
    real sign-in session. If someone ever widened a scope by mistake, tokens still could not mint
    tokens."""
    monkeypatch.setitem(token_utils.SCOPE_RULES, "alerts", token_utils.SCOPE_RULES["alerts"] + [("*", r"/api/tokens(/.*)?")])
    h = bearer(mint(http, "wide", ["alerts"])["token"])
    assert token_utils.allowed(["alerts"], "POST", "/api/tokens")  # the scope check alone would now pass...
    assert http.post("/api/tokens", headers=h, json={"name": "evil", "scopes": ["alerts"]}).status_code == 403
    assert http.get("/api/tokens", headers=h).status_code == 403
