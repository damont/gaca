"""A fake GACA Cloud and the fixtures to link a test server to it.

`FakeCloud` is an httpx.MockTransport handler: tests script answers per (method, path), the fake
records every request it got (so tests can assert what was forwarded, with which headers), and it
can play "down" (connection error) or "slow" (read timeout). Nothing here touches the network.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from gaca_api.config import settings
from gaca_api.main import app
from gaca_api.models.base import utcnow
from gaca_api.services import cloud_client, cloud_forward, cloud_list, pairing

CLOUD_URL = "https://cloud.test/api/v1"
SERVER_TOKEN = "gsrv_SECRET_TOKEN_VALUE_1234567890"
DEVICE_CODE = "DEVICE_CODE_SECRET_abcdef"
CODE = "ABCD2345EFGH6789"


def gacabit(gid="g1", title="Shared list", role="editor", owner="alice", version=1, rev=3, **extra):
    return {
        "id": gid, "title": title, "description": None, "version": version, "rev": rev, "state_version": 1,
        "role": role, "status": "active", "member_count": 2, "created_at": "2026-11-01T10:00:00Z",
        "updated_at": "2026-11-02T10:00:00Z", "owner": {"id": "acct_" + owner, "handle": owner},
        "link": f"gaca://gacabit/{gid}", **extra,
    }


class FakeCloud:
    def __init__(self):
        self.routes: dict[tuple[str, str], object] = {}
        self.requests: list[httpx.Request] = []
        self.down = False  # connection error
        self.slow = False  # read timeout
        self.redirect_to: str | None = None
        self.timeouts: list[dict] = []

    def on(self, method, path, status=200, json=None, headers=None, content=None):
        self.routes[(method, path)] = (status, json, headers or {}, content)
        return self

    def on_fn(self, method, path, fn):
        self.routes[(method, path)] = fn

    def seen(self, method=None, path=None):
        return [r for r in self.requests
                if (method is None or r.method == method) and (path is None or r.url.path == "/api/v1" + path)]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.timeouts.append(request.extensions.get("timeout", {}))
        if self.down:
            raise httpx.ConnectError("boom " + str(request.url), request=request)
        if self.slow:
            raise httpx.ReadTimeout("slow " + str(request.url), request=request)
        if self.redirect_to:
            return httpx.Response(302, headers={"location": self.redirect_to})
        path = request.url.path.removeprefix("/api/v1")
        route = self.routes.get((request.method, path))
        if callable(route):
            return route(request)
        if route is None:
            return httpx.Response(404, json={"code": "not_found", "message": "no such thing"})
        status, body, headers, content = route
        if content is not None:
            return httpx.Response(status, content=content, headers=headers)
        if body is None:
            return httpx.Response(status, headers=headers)
        return httpx.Response(status, json=body, headers=headers)


@pytest.fixture
def cloud():
    return FakeCloud()


@pytest.fixture
def http(tmp_path, monkeypatch, cloud):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "t.db"))
    monkeypatch.setattr(settings, "agent_engine", "hermes")
    monkeypatch.setattr(settings, "cloud_url", CLOUD_URL)
    monkeypatch.setattr(settings, "cloud_key", "")
    monkeypatch.setattr(cloud_client, "transport_override", httpx.MockTransport(cloud))
    cloud_client._clients.clear()
    cloud_list._synced.clear()
    cloud_forward.forget_pages()
    with TestClient(app) as client:
        client.db = str(tmp_path / "t.db")
        con = sqlite3.connect(client.db)
        state = {"hash": pairing._digest(CODE), "expires_at": (utcnow() + timedelta(hours=1)).isoformat(), "wrong": 0}
        con.execute("INSERT INTO server_meta (key, value) VALUES ('pair_code', ?)", (json.dumps(state),))
        con.commit()
        con.close()
        made = client.post("/api/devices/pair", json={"code": CODE, "name": "Test phone"})
        client.session = {"Authorization": "Bearer " + made.json()["access_token"]}
        yield client
    cloud_client._clients.clear()


def mint(http, name="hermes-test", scopes=("artifacts", "cloud")):
    r = http.post("/api/tokens", headers=http.session, json={"name": name, "scopes": list(scopes)})
    assert r.status_code == 201, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def script_link(cloud, *, server_id="srv1", scope="manage", handle="david", account_id="acct_david"):
    cloud.on("POST", "/auth/device/start", json={
        "device_code": DEVICE_CODE, "user_code": "ABCD-1234",
        "verification_uri": "https://cloud.test/link", "expires_in": 600, "interval": 5})
    cloud.on("POST", "/auth/device/token", json={
        "server_token": SERVER_TOKEN, "server": {"id": server_id, "name": "n", "scope": scope, "created_at": "x"},
        "account": {"id": account_id, "handle": handle, "email": "d@example.com", "created_at": "x"}})


def link(http, cloud, **kw):
    """Run the whole link flow against the fake cloud (the user 'approves' at once)."""
    script_link(cloud, **kw)
    assert http.post("/api/cloud/link/start", headers=http.session, json={}).status_code == 200
    _skip_interval(http)
    r = http.post("/api/cloud/link/poll", headers=http.session)
    assert r.json()["status"] == "linked", r.text
    return r.json()


def _skip_interval(http):
    """The poll interval is honoured; tests do not wait for it, they move the clock in the row."""
    con = sqlite3.connect(http.db)
    con.execute("UPDATE cloud_link SET pending_next_poll = '2000-01-01T00:00:00+00:00'")
    con.commit()
    con.close()


def raw_db_bytes(http) -> bytes:
    """Everything on disk for the database (main file and WAL), to search for secrets."""
    import pathlib

    out = b""
    for suffix in ("", "-wal"):
        p = pathlib.Path(http.db + suffix)
        if p.exists():
            out += p.read_bytes()
    return out
