"""HTTP-level checks for POST /api/auth/google: which audiences are accepted."""
import pytest
from fastapi.testclient import TestClient

from gaca_api.config import settings
from gaca_api.main import app
from gaca_api.routers import auth as auth_router


@pytest.fixture
def verify_calls(tmp_path, monkeypatch):
    """Stand in for Google's verifier: record the audience it was asked to
    accept and return a valid identity, so no network or real token is needed."""
    settings.db_path = str(tmp_path / "t.db")
    calls = []

    def fake_verify(token, request, audience=None):
        calls.append(audience)
        return {"sub": "g-1", "email": "d@example.com", "email_verified": True, "name": "D"}

    monkeypatch.setattr(auth_router.google_id_token, "verify_oauth2_token", fake_verify)
    return calls


def test_accepts_both_the_web_and_ios_client_ids(verify_calls, monkeypatch):
    monkeypatch.setattr(settings, "google_client_id", "web.apps.googleusercontent.com")
    monkeypatch.setattr(settings, "google_ios_client_id", "ios.apps.googleusercontent.com")
    with TestClient(app) as http:
        resp = http.post("/api/auth/google", json={"id_token": "x"})
    assert resp.status_code == 200
    assert verify_calls == [["web.apps.googleusercontent.com", "ios.apps.googleusercontent.com"]]


def test_ios_client_id_alone_is_enough_to_enable_google_login(verify_calls, monkeypatch):
    monkeypatch.setattr(settings, "google_client_id", None)
    monkeypatch.setattr(settings, "google_ios_client_id", "ios.apps.googleusercontent.com")
    with TestClient(app) as http:
        assert http.post("/api/auth/google", json={"id_token": "x"}).status_code == 200
    assert verify_calls == [["ios.apps.googleusercontent.com"]]


def test_neither_client_id_means_not_configured(verify_calls, monkeypatch):
    monkeypatch.setattr(settings, "google_client_id", None)
    monkeypatch.setattr(settings, "google_ios_client_id", None)
    with TestClient(app) as http:
        assert http.post("/api/auth/google", json={"id_token": "x"}).status_code == 500
    assert verify_calls == []
