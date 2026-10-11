"""First-run setup over HTTP: one owner per server, claimed once with a one-time code.

The setup code is issued by `gaca-api setup-code` on the server, never over HTTP, so these tests
plant one straight into the database file (hashed, exactly as the real thing stores it).
"""
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from gaca_api.config import settings
from gaca_api.main import app
from gaca_api.models.base import utcnow
from gaca_api.services import setup as setup_service
from gaca_api.utils.google import GoogleAuthError

CODE = "ABCD2345EFGH6789"


@pytest.fixture
def http(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "t.db"))
    monkeypatch.setattr(settings, "agent_engine", "hermes")
    monkeypatch.setattr(settings, "google_client_id", None)
    with TestClient(app) as client:
        client.db = str(tmp_path / "t.db")
        yield client


def plant_code(db: str, raw: str = CODE, minutes: int = 60, wrong: int = 0) -> None:
    import json

    state = {"hash": setup_service._digest(raw), "expires_at": (utcnow() + timedelta(minutes=minutes)).isoformat(), "wrong": wrong}
    con = sqlite3.connect(db)
    con.execute("INSERT OR REPLACE INTO server_meta (key, value) VALUES ('setup_code', ?)", (json.dumps(state),))
    con.commit()
    con.close()


def count(db: str, table: str = "users") -> int:
    con = sqlite3.connect(db)
    try:
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        con.close()


def claim(http, code=CODE, email="owner@example.com", password="correct horse"):
    return http.post("/api/setup/claim", json={"code": code, "name": "Owner", "email": email, "password": password})


# ── status ───────────────────────────────────────────────────────────

def test_status_says_unclaimed_then_claimed_and_is_public(http):
    first = http.get("/api/setup")
    assert first.status_code == 200
    assert first.json() == {"name": "gaca-api", "version": app.version, "claimed": False,
                            "sign_in": ["password"], "google_client_id": None}
    plant_code(http.db)
    assert claim(http).status_code == 201
    assert http.get("/api/setup").json()["claimed"] is True


def test_status_offers_google_only_when_configured(http, monkeypatch):
    monkeypatch.setattr(settings, "google_client_id", "abc.apps.googleusercontent.com")
    body = http.get("/api/setup").json()
    assert body["sign_in"] == ["password", "google"] and body["google_client_id"] == "abc.apps.googleusercontent.com"


# ── claiming ─────────────────────────────────────────────────────────

def test_claim_creates_the_owner_who_can_then_sign_in_anywhere(http):
    plant_code(http.db)
    made = claim(http)
    assert made.status_code == 201
    token = made.json()["access_token"]
    assert http.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"}).json()["email"] == "owner@example.com"
    # A second device only needs the URL and the credentials — no code.
    login = http.post("/api/auth/login", json={"email": "owner@example.com", "password": "correct horse"})
    assert login.status_code == 200
    assert count(http.db, "server_meta") == 0  # the code is spent


def test_a_claimed_server_refuses_every_further_claim_even_with_a_fresh_code(http):
    plant_code(http.db)
    assert claim(http).status_code == 201
    plant_code(http.db, raw="ZZZZ2222YYYY3333")
    again = claim(http, code="ZZZZ2222YYYY3333", email="intruder@example.com")
    assert again.status_code == 409
    assert count(http.db) == 1


def test_claim_needs_a_code_to_exist(http):
    assert claim(http).status_code == 403
    assert count(http.db) == 0


def test_the_code_is_stored_hashed_never_in_the_clear(http):
    plant_code(http.db)
    con = sqlite3.connect(http.db)
    stored = con.execute("SELECT value FROM server_meta").fetchone()[0]
    con.close()
    assert CODE not in stored and "ABCD" not in stored


def test_the_code_is_forgiving_about_case_and_dashes(http):
    plant_code(http.db)
    assert claim(http, code="abcd-2345 efgh-6789").status_code == 201


def test_a_wrong_code_is_refused_and_enough_wrong_ones_burn_the_real_one(http, monkeypatch):
    monkeypatch.setattr(settings, "auth_attempts_per_minute", 1000)  # this test is about burning, not the per-minute limit
    plant_code(http.db)
    for _ in range(setup_service.MAX_WRONG_ATTEMPTS - 1):
        assert claim(http, code="WRONG-WRONG-WRONG").status_code == 401
    assert claim(http, code="WRONG-WRONG-WRONG").status_code == 403  # the fifth burns it
    assert claim(http).status_code == 403  # the right code no longer works either
    assert count(http.db) == 0


def test_an_expired_code_is_refused_and_removed(http):
    plant_code(http.db, minutes=-1)
    assert claim(http).status_code == 403
    assert count(http.db, "server_meta") == 0


def test_a_weak_password_or_bad_email_is_rejected_without_spending_the_code(http):
    plant_code(http.db)
    assert claim(http, password="short").status_code == 422
    assert claim(http, email="not-an-email").status_code == 422
    assert claim(http).status_code == 201


def test_two_simultaneous_claims_make_exactly_one_owner(http):
    plant_code(http.db)
    with ThreadPoolExecutor(6) as pool:
        codes = list(pool.map(lambda i: claim(http, email=f"p{i}@example.com").status_code, range(6)))
    assert codes.count(201) == 1 and set(codes) <= {201, 409}
    assert count(http.db) == 1


# ── the other doors ──────────────────────────────────────────────────

def test_open_registration_is_closed(http):
    r = http.post("/api/auth/register", json={"name": "X", "email": "x@example.com", "password": "password123"})
    assert r.status_code == 403 and "closed" in r.json()["detail"].lower()
    assert count(http.db) == 0
    assert "/api/auth/register" not in app.openapi()["paths"]


def _google(monkeypatch, sub="g-1", email="owner@example.com", name="Owner", error=None):
    def fake(raw):
        if error:
            raise error
        return {"sub": sub, "email": email, "name": name}
    monkeypatch.setattr("gaca_api.routers.auth.verify_google_identity", fake)
    monkeypatch.setattr("gaca_api.routers.setup.verify_google_identity", fake)


def test_google_sign_in_never_creates_an_account(http, monkeypatch):
    _google(monkeypatch, email="stranger@gmail.com")
    r = http.post("/api/auth/google", json={"id_token": "t"})
    assert r.status_code == 403
    assert count(http.db) == 0
    plant_code(http.db)
    assert claim(http).status_code == 201
    r = http.post("/api/auth/google", json={"id_token": "t"})  # still the stranger
    assert r.status_code == 403 and count(http.db) == 1


def test_google_sign_in_links_to_the_owner_by_verified_email(http, monkeypatch):
    plant_code(http.db)
    assert claim(http).status_code == 201
    _google(monkeypatch, sub="g-owner", email="owner@example.com")
    first = http.post("/api/auth/google", json={"id_token": "t"})
    assert first.status_code == 200
    # Later the email could change at Google; the stable `sub` still identifies the owner.
    _google(monkeypatch, sub="g-owner", email="renamed@example.com")
    assert http.post("/api/auth/google", json={"id_token": "t"}).status_code == 200


def test_claiming_with_google_needs_the_code_and_then_works_for_sign_in(http, monkeypatch):
    _google(monkeypatch, sub="g-9", email="me@gmail.com", name="Me")
    no_code = http.post("/api/setup/claim/google", json={"code": CODE, "id_token": "t"})
    assert no_code.status_code == 403 and count(http.db) == 0
    plant_code(http.db)
    wrong = http.post("/api/setup/claim/google", json={"code": "NOPE-NOPE", "id_token": "t"})
    assert wrong.status_code == 401 and count(http.db) == 0
    ok = http.post("/api/setup/claim/google", json={"code": CODE, "id_token": "t"})
    assert ok.status_code == 201
    assert http.post("/api/auth/google", json={"id_token": "t"}).status_code == 200
    # ...and an owner who has only Google has no password to guess.
    assert http.post("/api/auth/login", json={"email": "me@gmail.com", "password": "anything123"}).status_code == 401


def test_a_bad_google_token_is_refused_before_the_code_is_touched(http, monkeypatch):
    plant_code(http.db)
    _google(monkeypatch, error=GoogleAuthError(401, "Invalid Google token"))
    r = http.post("/api/setup/claim/google", json={"code": CODE, "id_token": "t"})
    assert r.status_code == 401
    assert count(http.db, "server_meta") == 1  # code intact: a forged token cannot burn it


def test_signing_in_does_not_depend_on_email_capitalisation(http):
    plant_code(http.db)
    assert claim(http, email="David.Owner@Example.com").status_code == 201
    for typed in ("david.owner@example.com", "David.Owner@Example.com", "  DAVID.OWNER@EXAMPLE.COM "):
        ok = http.post("/api/auth/login", json={"email": typed.strip(), "password": "correct horse"})
        assert ok.status_code == 200, typed
    con = sqlite3.connect(http.db)
    assert con.execute("SELECT email FROM users").fetchone()[0] == "david.owner@example.com"
    con.close()


def test_a_code_that_expires_this_instant_is_expired(http):
    plant_code(http.db, minutes=0)
    assert claim(http).status_code == 403
