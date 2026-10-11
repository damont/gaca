"""What used to be nginx's job, now the API's own: headers, request size, rate limits and CORS."""
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.requests import Request as StarletteRequest

from gaca_api import security
from gaca_api.config import settings
from gaca_api.main import app
from gaca_api.security import BodyLimitMiddleware, RateLimiter, SecurityHeadersMiddleware, client_ip


@pytest.fixture
def http(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "t.db"))
    monkeypatch.setattr(settings, "agent_engine", "hermes")
    with TestClient(app) as client:
        yield client


# ── headers ──────────────────────────────────────────────────────────

def test_every_response_carries_the_hardening_headers_including_errors(http):
    for response in (http.get("/api/health"), http.get("/api/nope"), http.get("/api/crew")):  # 200, 404, 401
        h = response.headers
        assert h["x-content-type-options"] == "nosniff"
        assert h["x-frame-options"] == "DENY"
        assert h["referrer-policy"] == "strict-origin-when-cross-origin"
        assert h["permissions-policy"] == "camera=(), microphone=(), geolocation=()"


def test_a_route_that_sets_its_own_header_keeps_it():
    inner = FastAPI()

    @inner.get("/x")
    def x():
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse("hi", headers={"X-Frame-Options": "SAMEORIGIN"})

    inner.add_middleware(SecurityHeadersMiddleware)
    r = TestClient(inner).get("/x")
    assert r.headers.get_list("x-frame-options") == ["SAMEORIGIN"]
    assert r.headers["x-content-type-options"] == "nosniff"


# ── request size ─────────────────────────────────────────────────────

def test_an_oversized_body_is_refused_before_it_reaches_the_app(http):
    big = b"x" * (settings.max_body_bytes + 1)
    r = http.post("/api/devices/pair", content=big, headers={"content-type": "application/json"})
    assert r.status_code == 413 and r.json() == {"detail": "Request body is too large"}
    assert r.headers["x-content-type-options"] == "nosniff"


def test_a_body_just_under_the_limit_gets_through(http):
    ok = b"{" + b" " * (settings.max_body_bytes - 100) + b"}"
    r = http.post("/api/devices/pair", content=ok, headers={"content-type": "application/json"})
    assert r.status_code == 422  # reached the validator, not the size gate


def test_a_streamed_body_without_a_content_length_is_still_capped(http):
    def chunks():
        for _ in range(40):
            yield b"x" * (100 * 1024)  # 4 MiB, no Content-Length
    r = http.post("/api/devices/pair", content=chunks(), headers={"content-type": "application/json"})
    assert r.status_code == 413


def test_a_lying_content_length_cannot_smuggle_more_than_the_limit():
    inner = FastAPI()

    @inner.post("/up")
    async def up(request: Request):
        return {"n": len(await request.body())}

    inner.add_middleware(BodyLimitMiddleware, max_bytes=1000)
    c = TestClient(inner)
    assert c.post("/up", content=b"x" * 900).json() == {"n": 900}
    assert c.post("/up", content=b"x" * 1001).status_code == 413


def test_photo_uploads_at_the_photo_cap_still_fit_under_the_body_limit():
    from gaca_api.services import attachments
    assert settings.max_body_bytes > attachments.MAX_BYTES + 64 * 1024  # room for multipart framing


# ── rate limiting ────────────────────────────────────────────────────

def test_limiter_allows_the_limit_then_says_how_long_to_wait():
    now = [0.0]
    lim = RateLimiter(clock=lambda: now[0])
    assert all(lim.hit("a", 3, 60) is None for _ in range(3))
    wait = lim.hit("a", 3, 60)
    assert wait is not None and 59 <= wait <= 60
    now[0] = 30
    assert 29 <= lim.hit("a", 3, 60) <= 30
    now[0] = 60.5  # the first hits have aged out
    assert lim.hit("a", 3, 60) is None


def test_limiter_keys_are_independent_and_memory_is_bounded():
    now = [0.0]
    lim = RateLimiter(clock=lambda: now[0])
    for _ in range(3):
        lim.hit("a", 3, 60)
    assert lim.hit("a", 3, 60) is not None and lim.hit("b", 3, 60) is None
    for i in range(10_100):
        lim.hit(f"flood-{i}", 3, 1)
        now[0] += 0.001
    now[0] += 5
    lim.hit("x", 3, 1)
    assert len(lim._hits) < 10_000


def test_pairing_is_rate_limited_per_client(http, monkeypatch):
    monkeypatch.setattr(settings, "auth_attempts_per_minute", 3)
    body = {"code": "AAAA-BBBB", "name": "Phone"}
    codes = [http.post("/api/devices/pair", json=body).status_code for _ in range(5)]
    assert codes == [403, 403, 403, 429, 429]
    r = http.post("/api/devices/pair", json=body)
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1 and "Too many attempts" in r.json()["detail"]


def _request(peer, headers=None):
    scope = {"type": "http", "client": (peer, 1234) if peer else None, "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]}
    return StarletteRequest(scope)


def test_the_real_client_ip_comes_from_cloudflare_only_when_the_peer_is_local():
    assert client_ip(_request("127.0.0.1", {"CF-Connecting-IP": "203.0.113.9"})) == "203.0.113.9"
    assert client_ip(_request("::1", {"CF-Connecting-IP": "2001:db8::7"})) == "2001:db8::7"
    # A direct (non-local) caller cannot pick its own identity by sending the header.
    assert client_ip(_request("198.51.100.4", {"CF-Connecting-IP": "203.0.113.9"})) == "198.51.100.4"
    # Garbage in the header is ignored.
    assert client_ip(_request("127.0.0.1", {"CF-Connecting-IP": "not-an-ip; DROP"})) == "127.0.0.1"
    assert client_ip(_request("127.0.0.1")) == "127.0.0.1"
    assert client_ip(_request(None)) == "unknown"


# ── CORS ─────────────────────────────────────────────────────────────

def test_cross_origin_calls_get_no_cors_permission_by_default(http):
    r = http.options("/api/health", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"})
    assert "access-control-allow-origin" not in r.headers
    assert settings.cors_origins == ""


def test_cors_origin_list_parsing():
    assert security.cors_origins("") == []
    assert security.cors_origins(" https://a.example/ , https://b.example ,, ") == ["https://a.example", "https://b.example"]
