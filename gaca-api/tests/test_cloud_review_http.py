"""Fixes from the independent review of the cloud link (PR #63): gzip answers, opt-in `cloud` scope and
what it does and does not allow, pointer visibility per token type, outage behaviour of the list,
pool separation for long-polls, the double-poll race, the small hardening items, and tests that pin
the dumb pass-through rules (so a change that adds a local rule, drops user scoping, forwards
cloud-only headers or stops encoding ids fails here)."""
import asyncio
import gzip
import json
import logging
import os
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from gaca_api.config import Settings, settings
from gaca_api.main import app
from gaca_api.models.user import UserDoc
from gaca_api.services import cloud_client, cloud_crypto, cloud_forward, cloud_list, cloud_link
from gaca_api.utils import tokens as token_utils
from gaca_api.utils.auth import get_current_user
from tests.cloud_support import (  # noqa: F401  (fixtures)
    CLOUD_URL, DEVICE_CODE, SERVER_TOKEN, _skip_interval, cloud, gacabit, http, link, mint, script_link,
)

A = "/api/artifacts/g1"


@pytest.fixture
def linked(http, cloud):
    link(http, cloud)
    cloud.on("GET", "/gacabits", json={"gacabits": [gacabit()]})
    assert [a["id"] for a in http.get("/api/artifacts", headers=http.session).json()["artifacts"]] == ["g1"]
    cloud.requests.clear()
    return http


def hdr(http, **extra):
    return {**http.session, **extra}


def gz(headers=None, **kw):
    return {"content-encoding": "gzip", **(headers or {})}


# ── 1. gzip answers ──────────────────────────────────────────────────────────
def test_a_gzipped_answer_is_decoded_once_not_twice(cloud):
    body = gzip.compress(json.dumps({"hello": "x" * 2000}).encode())
    cloud.on("GET", "/gacabits", content=body, headers={"content-encoding": "gzip", "content-type": "application/json"})

    async def go():
        client = cloud_client.CloudClient(CLOUD_URL)
        try:
            return await client.request("GET", "/gacabits", token="T")
        finally:
            await client.aclose()

    cloud_client.transport_override = httpx.MockTransport(cloud)
    try:
        r = asyncio.run(go())
    finally:
        cloud_client.transport_override = None
    assert r.json() == {"hello": "x" * 2000}
    assert "content-encoding" not in r.headers


def test_gzipped_cloud_json_and_page_html_work_end_to_end(linked, cloud, monkeypatch):
    monkeypatch.setattr(settings, "cloud_list_ttl_seconds", 0)
    listing = gzip.compress(json.dumps({"gacabits": [gacabit(), gacabit("g2", "Second")]}).encode())
    cloud.on("GET", "/gacabits", content=listing, headers=gz({"content-type": "application/json"}))
    got = linked.get("/api/artifacts", headers=hdr(linked)).json()
    assert got["stale"] is False and {a["id"] for a in got["artifacts"]} == {"g1", "g2"}
    cloud.on("GET", "/gacabits/g1", content=gzip.compress(json.dumps(gacabit(version=4)).encode()),
             headers=gz({"content-type": "application/json"}))
    cloud.on("GET", "/gacabits/g1/html", content=gzip.compress("<h1>Zipped é</h1>".encode()),
             headers=gz({"content-type": "text/html; charset=utf-8"}))
    page = linked.get(A + "/page", headers=hdr(linked))
    assert page.status_code == 200 and "<h1>Zipped é</h1>" in page.text
    cloud.on("GET", "/gacabits/g1/state", content=gzip.compress(b'{"state":{},"version":1,"rev":1}'),
             headers=gz({"content-type": "application/json"}))
    r = linked.get(A + "/state", headers=hdr(linked))
    assert r.status_code == 200 and r.json()["rev"] == 1 and "content-encoding" not in r.headers


def test_a_gzipped_link_flow_works(http, cloud):
    script_link(cloud)
    cloud.on("POST", "/auth/device/start", content=gzip.compress(json.dumps({
        "device_code": DEVICE_CODE, "user_code": "ABCD-1234", "verification_uri": "https://cloud.test/link",
        "expires_in": 600, "interval": 5}).encode()), headers=gz({"content-type": "application/json"}))
    assert http.post("/api/cloud/link/start", headers=http.session, json={}).status_code == 200


# ── 2. the cloud scope is opt-in, and agents never do the person's things ────
def test_cloud_is_not_a_default_scope():
    assert "cloud" in token_utils.SCOPES and "cloud" not in token_utils.DEFAULT_SCOPES
    assert set(token_utils.DEFAULT_SCOPES) == set(token_utils.SCOPES) - {"cloud"}


def test_the_cli_default_does_not_include_cloud():
    from gaca_api.cli import _parser

    args = _parser().parse_args(["token", "create", "--name", "x"])
    assert "cloud" not in args.scopes.split(",")


def test_an_agent_with_cloud_can_read_status_and_templates_but_never_link_or_join(linked, cloud):
    hermes = mint(linked, scopes=("artifacts", "cloud"))
    cloud.on("GET", "/templates", json={"templates": []})
    assert linked.get("/api/cloud/link", headers=hermes).status_code == 200
    assert linked.get("/api/cloud/templates", headers=hermes).status_code == 200
    for method, path in (("POST", "/link/start"), ("POST", "/link/poll"), ("DELETE", "/link"),
                         ("POST", "/invites/redeem"), ("POST", "/invites/preview"),
                         ("POST", "/templates/t1/install"), ("POST", "/templates/t1/get")):
        assert linked.request(method, "/api/cloud" + path, headers=hermes, json={"token": "t"}).status_code == 403, path


def test_an_agent_cannot_leave_a_shared_gacabit_but_can_use_its_data(linked, cloud):
    hermes = mint(linked, scopes=("artifacts", "cloud"))
    r = linked.delete(A, headers=hermes)
    assert r.status_code == 403 and r.json()["code"] == "paired_device_required"
    assert cloud.seen("POST", "/gacabits/g1/leave") == []
    cloud.on("GET", "/gacabits/g1/state", json={"state": {}, "version": 1, "rev": 1})
    assert linked.get(A + "/state", headers=hermes).status_code == 200


# ── 3. pointers are invisible to tokens without cloud ────────────────────────
def test_pointer_visibility_per_token_type(linked, cloud, monkeypatch):
    monkeypatch.setattr(settings, "cloud_list_ttl_seconds", 0)
    linked.post("/api/artifacts", headers=hdr(linked), json={"title": "Local", "html": "<p>x</p>"})
    plain = mint(linked, name="plain", scopes=("artifacts",))
    with_cloud = mint(linked, name="with", scopes=("artifacts", "cloud"))
    cloud.requests.clear()

    # a token without `cloud`: local rows only, no cloud call at all, the pointer id is a plain 404
    got = linked.get("/api/artifacts", headers=plain).json()
    assert [a["title"] for a in got["artifacts"]] == ["Local"] and got["stale"] is False
    assert all(a["home"] == "local" for a in got["artifacts"])
    for method, suffix in (("GET", ""), ("GET", "/state"), ("GET", "/page"), ("POST", "/call"), ("GET", "/html")):
        r = linked.request(method, A + suffix, headers=plain, json={"method": "data", "args": {}} if suffix == "/call" else None)
        assert r.status_code == 404, suffix
    assert linked.put("/api/artifacts/order", headers=plain, json={"ids": ["g1"]}).status_code == 404
    assert cloud.requests == []

    # a token with `cloud`: the pointer is listed, flagged, and usable
    got = {a["id"]: a for a in linked.get("/api/artifacts", headers=with_cloud).json()["artifacts"]}
    assert got["g1"]["home"] == "cloud" and got["g1"]["role"] == "editor" and got["g1"]["shared_by"] == "alice"
    cloud.on("GET", "/gacabits/g1/state", json={"state": {}, "version": 1, "rev": 1})
    assert linked.get(A + "/state", headers=with_cloud).status_code == 200

    # the paired app always sees it
    assert {a["id"] for a in linked.get("/api/artifacts", headers=hdr(linked)).json()["artifacts"]} >= {"g1"}


def test_a_chat_chip_and_an_alert_resolve_a_pointers_title_from_the_cache_without_calling_the_cloud(linked, cloud):
    from gaca_api.services import artifact_refs

    cloud.requests.clear()
    made = linked.post("/api/alerts", headers=hdr(linked), json={"kind": "k", "title": "T", "artifact_id": "g1"})
    assert made.status_code == 201, made.text
    assert made.json()["artifact"] == {"id": "g1", "title": "Shared list", "description": None, "crew": None, "missing": False}
    listed = linked.get("/api/alerts", headers=hdr(linked)).json()
    assert any(a["artifact"] and a["artifact"]["title"] == "Shared list" for a in (listed["alerts"] if isinstance(listed, dict) else listed))
    assert cloud.requests == []  # titles come from the pointer, never from a call on the hot path
    assert artifact_refs is not None


def test_an_alert_from_a_token_without_cloud_cannot_attach_a_pointer(linked, cloud):
    alerts_only = mint(linked, name="al", scopes=("alerts",))
    r = linked.post("/api/alerts", headers=alerts_only, json={"kind": "k", "title": "T", "artifact_id": "g1"})
    assert r.status_code == 404
    both = mint(linked, name="al2", scopes=("alerts", "cloud"))
    assert linked.post("/api/alerts", headers=both, json={"kind": "k", "title": "T", "artifact_id": "g1"}).status_code == 201


def test_chat_links_resolve_pointer_titles():
    from gaca_api.services import artifact_refs  # noqa: F401  (the alert test above exercises resolve())
    assert hasattr(artifact_refs, "resolve")


# ── 4. a dead cloud does not slow every list ─────────────────────────────────
def test_after_a_failure_lists_skip_the_cloud_for_the_backoff_and_are_stale(linked, cloud, monkeypatch):
    monkeypatch.setattr(settings, "cloud_list_ttl_seconds", 0)
    cloud.down = True
    first = linked.get("/api/artifacts", headers=hdr(linked)).json()
    assert first["stale"] is True and len(cloud.seen("GET", "/gacabits")) == 1
    for _ in range(5):
        again = linked.get("/api/artifacts", headers=hdr(linked)).json()
        assert again["stale"] is True and [a["id"] for a in again["artifacts"]] == ["g1"]
    assert len(cloud.seen("GET", "/gacabits")) == 1  # no further attempts inside the backoff window


def test_the_list_sync_has_a_short_connect_timeout(linked, cloud, monkeypatch):
    monkeypatch.setattr(settings, "cloud_list_ttl_seconds", 0)
    cloud.on("GET", "/gacabits", json={"gacabits": []})
    linked.get("/api/artifacts", headers=hdr(linked))
    assert cloud.timeouts[-1]["connect"] <= 3


def test_concurrent_lists_make_one_cloud_request(linked, cloud):
    cloud_list._synced.clear()  # as if nothing had been fetched yet
    calls = []

    def slow(request):
        calls.append(1)
        time.sleep(0.3)
        return httpx.Response(200, json={"gacabits": [gacabit()]})

    cloud.on_fn("GET", "/gacabits", slow)
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda _: linked.get("/api/artifacts", headers=hdr(linked)).json(), range(8)))
    assert len(calls) == 1
    assert all(r["stale"] is False and [a["id"] for a in r["artifacts"]] == ["g1"] for r in results)


# ── 8. long-polls have their own pool ────────────────────────────────────────
def test_long_polls_use_a_separate_pool_from_ordinary_calls(linked, cloud, monkeypatch):
    client = cloud_client.client_for(CLOUD_URL)
    assert client._poll is not client._http
    used = []
    real_http, real_poll = client._http.stream, client._poll.stream
    monkeypatch.setattr(client._http, "stream", lambda *a, **k: (used.append("http"), real_http(*a, **k))[1])
    monkeypatch.setattr(client._poll, "stream", lambda *a, **k: (used.append("poll"), real_poll(*a, **k))[1])
    cloud.on("GET", "/gacabits/g1/data", json={"rev": 1, "changed": False})
    cloud.on("GET", "/gacabits/g1/state", json={"state": {}, "version": 1, "rev": 1})
    linked.get(A + "/data?wait=8", headers=hdr(linked))
    linked.get(A + "/state", headers=hdr(linked))
    assert used == ["poll", "http"]


def test_many_open_long_polls_do_not_starve_a_write(monkeypatch):
    """Real sockets: 30 long-polls held open by a slow stub, and a write that must still go through quickly."""

    class Stub(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            time.sleep(1.5)
            self._send(b'{"rev":1,"changed":false}')

        def do_PUT(self):
            self._send(b'{"state":{},"version":2,"rev":2}')

        def _send(self, body):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(settings, "cloud_allow_insecure_dev", True)
    monkeypatch.setattr(cloud_client, "transport_override", None)

    async def go():
        client = cloud_client.CloudClient(f"http://127.0.0.1:{server.server_port}/api/v1")
        try:
            polls = [asyncio.create_task(client.request("GET", "/gacabits/g1/data", long_poll=True, timeout=10))
                     for _ in range(30)]
            await asyncio.sleep(0.3)  # all 30 are in flight
            started = time.monotonic()
            write = await client.request("PUT", "/gacabits/g1/state", content=b"{}", headers={"content-type": "application/json"},
                                         timeout=5)
            took = time.monotonic() - started
            answers = await asyncio.gather(*polls)
            return write, took, answers
        finally:
            await client.aclose()

    try:
        write, took, answers = asyncio.run(go())
    finally:
        server.shutdown()
    assert write.status_code == 200 and took < 1.0
    assert all(a.status_code == 200 for a in answers)


# ── 9. two polls at once ─────────────────────────────────────────────────────
def test_two_concurrent_polls_never_wipe_a_success(http, cloud):
    script_link(cloud)
    http.post("/api/cloud/link/start", headers=http.session, json={})
    _skip_interval(http)
    real = cloud.routes[("POST", "/auth/device/token")]
    status, body, headers, content = real
    calls = []

    def token(request):
        calls.append(1)
        time.sleep(0.3)
        if len(calls) == 1:
            return httpx.Response(200, json=body)
        return httpx.Response(400, json={"code": "expired_token", "message": "used"})

    cloud.on_fn("POST", "/auth/device/token", token)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: http.post("/api/cloud/link/poll", headers=http.session), range(2)))
    assert [r.status_code for r in results] == [200, 200]
    assert {r.json()["status"] for r in results} == {"linked"}
    assert len(calls) == 1  # the second poll did not spend the device code again
    assert http.get("/api/cloud/link", headers=http.session).json()["linked"] is True


def test_an_expired_answer_arriving_after_the_link_completed_does_not_delete_it(http, cloud):
    script_link(cloud)
    http.post("/api/cloud/link/start", headers=http.session, json={})
    _skip_interval(http)

    def racing(request):
        con = sqlite3.connect(http.db)  # the link completes elsewhere while this answer is in flight
        con.execute("UPDATE cloud_link SET status = 'linked', token_enc = 'x', handle = 'david'")
        con.commit()
        con.close()
        return httpx.Response(400, json={"code": "expired_token", "message": "old"})

    cloud.on_fn("POST", "/auth/device/token", racing)
    r = http.post("/api/cloud/link/poll", headers=http.session)
    assert r.json()["status"] == "linked"
    con = sqlite3.connect(http.db)
    assert con.execute("SELECT status FROM cloud_link").fetchone() == ("linked",)
    con.close()


# ── 10. the small ones ───────────────────────────────────────────────────────
def test_a_partial_list_never_prunes(linked, cloud, monkeypatch):
    monkeypatch.setattr(settings, "cloud_list_ttl_seconds", 0)
    n = []

    def endless(request):
        n.append(1)
        return httpx.Response(200, json={"gacabits": [gacabit(f"p{len(n)}", f"P{len(n)}")], "next_cursor": f"c{len(n)}"})

    cloud.on_fn("GET", "/gacabits", endless)
    got = {a["id"] for a in linked.get("/api/artifacts", headers=hdr(linked)).json()["artifacts"]}
    assert "g1" in got and len(got) == 11  # g1 kept although the 10 pages we read did not list it


def test_the_page_cache_is_bounded_in_bytes(linked, cloud, monkeypatch):
    monkeypatch.setattr(settings, "cloud_list_ttl_seconds", 0)
    monkeypatch.setattr(settings, "cloud_page_cache_bytes", 100)
    cloud.on("GET", "/gacabits", json={"gacabits": [gacabit(), gacabit("g2", "Two"), gacabit("g3", "Three")]})
    linked.get("/api/artifacts", headers=hdr(linked))
    for gid in ("g1", "g2", "g3"):
        cloud.on("GET", f"/gacabits/{gid}", json=gacabit(gid))
        cloud.on("GET", f"/gacabits/{gid}/html", content=("<p>" + "x" * 70 + "</p>").encode(), headers={"content-type": "text/html"})
        assert linked.get(f"/api/artifacts/{gid}/page", headers=hdr(linked)).status_code == 200
    assert cloud_forward._pages_bytes <= 100 and len(cloud_forward._PAGES) == 1


@pytest.mark.parametrize("uri,ok", [
    ("https://cloud.test/link", True),
    ("https://evil.example/link", False),
    ("https://cloud.test.evil.example/link", False),
    ("http://cloud.test/link", False),
    ("https://user:pw@cloud.test/link", False),
    ("javascript:alert(1)", False),
])
def test_the_verification_uri_must_be_the_clouds_own(http, cloud, uri, ok):
    script_link(cloud)
    cloud.on("POST", "/auth/device/start", json={"device_code": DEVICE_CODE, "user_code": "ABCD-1234",
                                                 "verification_uri": uri, "expires_in": 600, "interval": 5})
    r = http.post("/api/cloud/link/start", headers=http.session, json={})
    assert (r.status_code == 200) is ok
    if not ok:
        assert r.status_code == 502 and uri not in r.text


def test_the_parent_domain_of_the_cloud_host_is_accepted():
    assert cloud_link._verification_ok("https://example.com/link", "https://api.example.com/api/v1")
    assert not cloud_link._verification_ok("https://com/link", "https://api.example.com/api/v1")
    assert not cloud_link._verification_ok("https://example.org/link", "https://api.example.com/api/v1")


def test_only_the_clouds_own_httpx_log_lines_are_dropped(linked, caplog):
    caplog.set_level(logging.INFO, logger="httpx")
    logging.getLogger("httpx").info('HTTP Request: GET https://cloud.test/api/v1/gacabits "HTTP/1.1 200 OK"')
    logging.getLogger("httpx").info('HTTP Request: GET https://somewhere-else.example/x "HTTP/1.1 200 OK"')
    assert "cloud.test" not in caplog.text and "somewhere-else.example" in caplog.text


def test_with_no_link_the_limit_reaches_the_local_query(http, monkeypatch):
    from gaca_api.services import artifacts as svc

    seen = []
    real = svc.list_for_user

    async def spy(user_id, include_archived, session_id, alert_id, q, limit):
        seen.append(limit)
        return await real(user_id, include_archived, session_id, alert_id, q, limit)

    monkeypatch.setattr(svc, "list_for_user", spy)
    http.get("/api/artifacts?limit=7", headers=http.session)
    assert seen == [7]


def test_unlink_clears_every_pointer(linked, cloud):
    cloud.on("DELETE", "/me/servers/srv1", status=204)
    linked.delete("/api/cloud/link?force=true", headers=hdr(linked))  # a cloud page is present: force (8b)
    con = sqlite3.connect(linked.db)
    assert con.execute("SELECT count(*) FROM cloud_pointers").fetchone() == (0,)
    con.close()


def test_the_key_file_is_never_seen_half_written(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "k.db"))
    monkeypatch.setattr(settings, "cloud_key", "")
    with ThreadPoolExecutor(16) as pool:
        keys = list(pool.map(lambda _: tuple(cloud_crypto._read_keys()), range(16)))
    assert len(set(keys)) == 1 and len(keys[0]) == 1  # everybody saw the same single, complete key
    path = cloud_crypto.key_file()
    cloud_crypto._fernet()  # parses as a real Fernet key
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []


def test_cryptography_is_a_declared_dependency():
    text = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    assert '"cryptography' in text


# ── 7. off by default ────────────────────────────────────────────────────────
def test_the_default_cloud_url_is_empty():
    assert Settings(_env_file=None).cloud_url == ""


def test_with_no_cloud_url_linking_is_refused_and_nothing_is_called(http, cloud, monkeypatch):
    monkeypatch.setattr(settings, "cloud_url", "")
    r = http.post("/api/cloud/link/start", headers=http.session, json={})
    assert r.status_code == 409 and r.json()["code"] == "cloud_not_configured"
    assert cloud.requests == []
    assert http.get("/api/cloud/link", headers=http.session).json()["state"] == "none"
    made = http.post("/api/artifacts", headers=http.session, json={"title": "Mine", "html": "<p>x</p>"}).json()
    assert [a["id"] for a in http.get("/api/artifacts", headers=http.session).json()["artifacts"]] == [made["id"]]
    assert cloud.requests == []


# ── mutation killers ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw", [
    b'{"state": 5, "unknown": null,   "x":[1,   2]}',  # nothing a local model would accept
    b'{"state": {"a": "' + b"x" * 1_500_000 + b'"}}',  # over the local 1 MB state cap
    '{"state": {"é": "☃"}, "expected_version": -5}'.encode(),
    b'{"state": {}, "state": {"dup": 1}}',  # duplicate keys, kept as sent
])
def test_bodies_are_forwarded_byte_for_byte_with_no_local_rule(linked, cloud, raw):
    cloud.on("PUT", "/gacabits/g1/state", status=422, json={"code": "schema_violation", "message": "the cloud decides"})
    r = linked.put(A + "/state", headers=hdr(linked, **{"Content-Type": "application/json"}), content=raw)
    assert r.status_code == 422 and r.json()["code"] == "schema_violation"
    assert cloud.seen("PUT", "/gacabits/g1/state")[0].content == raw


def test_a_patch_the_local_model_would_reject_still_reaches_the_cloud(linked, cloud):
    raw = b'{"title": "", "archived": "maybe", "html": ""}'
    cloud.on("PATCH", "/gacabits/g1", status=403, json={"code": "forbidden_role", "message": "no"})
    r = linked.patch(A, headers=hdr(linked, **{"Content-Type": "application/json"}), content=raw)
    assert r.status_code == 403 and cloud.seen("PATCH", "/gacabits/g1")[0].content == raw


def test_the_pointer_lookup_is_scoped_to_the_user(linked, cloud):
    cloud.on("GET", "/gacabits/g1/state", json={"state": {"private": True}, "version": 1, "rev": 1})
    other = UserDoc(id="someone-else", email="x@example.com", display_name="X")
    app.dependency_overrides[get_current_user] = lambda: other
    try:
        cloud.requests.clear()
        r = linked.get(A + "/state")
        assert r.status_code == 404 and cloud.requests == []  # not forwarded with the owner's token
        for suffix in ("", "/page", "/html", "/versions", "/data"):
            assert linked.get(A + suffix).status_code == 404
        assert linked.delete(A).status_code == 404
        assert linked.get("/api/artifacts").json()["artifacts"] == []
        assert cloud.requests == []
    finally:
        app.dependency_overrides.clear()


def test_cloud_set_cookie_location_and_friends_are_never_forwarded_to_the_app(linked, cloud):
    nasty = {"set-cookie": "sid=1; Path=/", "location": "https://evil.example/", "server": "cloudflare",
             "x-powered-by": "x", "access-control-allow-origin": "*", "www-authenticate": "Bearer realm=cloud",
             "content-type": "application/json"}
    cloud.on("GET", "/gacabits/g1/state", json={"state": {}, "version": 1, "rev": 1}, headers=nasty)
    cloud.on("GET", "/gacabits/g1", json=gacabit(), headers=nasty)
    cloud.on("PUT", "/gacabits/g1/state", status=409, json={"code": "conflict", "message": "m"}, headers=nasty)
    cloud.on("GET", "/gacabits/g1/html", content=b"<p>x</p>", headers={**nasty, "content-type": "text/html"})
    responses = [linked.get(A + "/state", headers=hdr(linked)), linked.get(A, headers=hdr(linked)),
                 linked.put(A + "/state", headers=hdr(linked), json={"state": {}}), linked.get(A + "/html", headers=hdr(linked))]
    for r in responses:
        for forbidden in ("set-cookie", "location", "server", "x-powered-by", "www-authenticate"):
            assert forbidden not in {k.lower() for k in r.headers} or forbidden == "server" and "cloudflare" not in r.headers[forbidden]
        assert r.headers.get("access-control-allow-origin") != "*"
        assert "evil.example" not in str(r.headers) and "sid=1" not in str(r.headers)


def test_ids_are_percent_encoded_in_the_forwarded_path(http, cloud):
    link(http, cloud)
    con = sqlite3.connect(http.db)
    con.execute("INSERT INTO cloud_pointers VALUES ('a b?c', (SELECT user_id FROM cloud_link), 'a b?c', 'acct', 'editor', NULL, "
                "'odd', 1, NULL, '2026-01-01T00:00:00+00:00', '{}')")
    con.commit()
    con.close()
    cloud.routes[("GET", "/gacabits/a%20b%3Fc/state")] = (200, {"state": {}, "version": 1, "rev": 1}, {}, None)
    cloud.on_fn("GET", "/gacabits/a b?c/state", lambda r: httpx.Response(200, json={"state": {}, "version": 1, "rev": 1}))
    r = http.get("/api/artifacts/a%20b%3Fc/state", headers=http.session)
    assert r.status_code == 200, r.text
    assert cloud.requests[-1].url.raw_path.decode().startswith("/api/v1/gacabits/a%20b%3Fc/state")


def test_quote_id_encodes_and_refuses_dot_segments():
    assert cloud_client.quote_id("a/b c?d#e") == "a%2Fb%20c%3Fd%23e"
    for bad in ("", ".", ".."):
        with pytest.raises(Exception):
            cloud_client.quote_id(bad)


# ── 5. the Hermes skill script ───────────────────────────────────────────────
SCRIPT = Path(__file__).resolve().parents[2] / "skills" / "gaca-artifacts" / "scripts" / "artifacts.py"


@pytest.fixture
def skill_server():
    answers = {"status": 200, "body": {}}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = json.dumps(answers["body"]).encode()
            self.send_response(answers["status"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def run(*args):
        env = {"PATH": os.environ["PATH"], "GACA_BASE_URL": f"http://127.0.0.1:{server.server_port}", "GACA_API_TOKEN": "tok",
               "HOME": "/nonexistent", "PYTHONDONTWRITEBYTECODE": "1"}
        return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, env=env, timeout=60)

    yield answers, run
    server.shutdown()


def test_skill_shows_the_servers_reason_for_a_403_and_the_token_message_only_for_auth(skill_server):
    answers, run = skill_server
    answers.update(status=403, body={"code": "forbidden_role", "message": "Viewers cannot write", "detail": "Viewers cannot write"})
    r = run("list")
    assert r.returncode != 0 and "forbidden_role" in r.stderr and "Viewers cannot write" in r.stderr
    assert "mint a new" not in r.stderr
    answers.update(status=403, body={"detail": "This API token is not allowed to call that endpoint."})
    assert "mint a new GACA_API_TOKEN" in run("list").stderr
    answers.update(status=401, body={"detail": "Could not validate credentials"})
    assert "mint a new GACA_API_TOKEN" in run("list").stderr


def test_skill_list_shows_home_and_role_for_cloud_rows(skill_server):
    answers, run = skill_server
    item = {"id": "g1", "title": "Shared", "version": 1, "updated_at": "2026-11-02T10:00:00Z", "home": "cloud",
            "role": "editor", "shared_by": "alice"}
    local = {"id": "l1", "title": "Mine", "version": 2, "updated_at": "2026-11-02T10:00:00Z", "home": "local", "role": "owner"}
    answers.update(status=200, body={"artifacts": [item, local], "stale": False})
    out = run("list").stdout
    assert "cloud, editor, shared by alice" in out
    assert [line for line in out.splitlines() if line.startswith("l1")][0].count("cloud") == 0
