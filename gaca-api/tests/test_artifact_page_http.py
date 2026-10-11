"""The artifact page as the API serves it (GET /page) and the page's calls back
(POST /call) — each call must answer exactly what its direct endpoint answers."""
import json
import re

import pytest
from fastapi.testclient import TestClient

from gaca_api.config import settings
from gaca_api.main import app
from gaca_api.models.user import UserDoc
from gaca_api.services import artifact_page
from gaca_api.utils.auth import get_current_user

_current = {"id": "u1"}


@pytest.fixture
def http(tmp_path, monkeypatch):
    settings.db_path = str(tmp_path / "t.db")
    monkeypatch.setattr(settings, "agent_engine", "hermes")  # lifespan needs one; never contacted
    _current["id"] = "u1"
    app.dependency_overrides[get_current_user] = lambda: UserDoc(
        id=_current["id"], email=f"{_current['id']}@example.com", display_name="Test"
    )
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def make(http, html="<h1>Costco</h1>", **kw):
    r = http.post("/api/artifacts", json={"title": "Costco </script>", "html": html, **kw})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def call(http, aid, method, **args):
    return http.post(f"/api/artifacts/{aid}/call", json={"method": method, "args": args})


# ── the page ─────────────────────────────────────────────────────────────────

def _prelude(aid, title):
    init = json.dumps(
        {"artifact": {"id": aid, "title": title}, "theme": {"dark": False, "accent": "#2D3C4C"}},
        separators=(",", ":"),
    ).replace("</", "<\\/")
    return (
        f'<meta http-equiv="Content-Security-Policy" content="{artifact_page.CSP}">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<style>{artifact_page._THEME_CSS}</style>"
        f"<script>window.__GACA_INIT={init};</script>"
        f"<script>{artifact_page.sdk().replace('</script', '<\\/script')}</script>"
    )


def test_page_is_wrapped_after_head_and_served_uncached(http):
    aid = make(http, html='<!doctype html><html><HEAD lang="x"><title>t</title></HEAD><body>hi</body></html>')
    r = http.get(f"/api/artifacts/{aid}/page")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["content-security-policy"] == artifact_page.CSP
    pre = _prelude(aid, "Costco </script>")
    assert r.text == f'<!doctype html><html><HEAD lang="x">{pre}<title>t</title></HEAD><body>hi</body></html>'
    # the title's "</" can't end the init script early
    assert "Costco <\\/script>" in r.text


def test_page_fallbacks_without_head():
    pre = _prelude("a1", "T")
    assert artifact_page.build("<html><body>x</body></html>", "a1", "T") == (
        f"<html><head>{pre}</head><body>x</body></html>"
    )
    assert artifact_page.build("<p>x</p>", "a1", "T") == (
        f'<!doctype html><html><head><meta charset="utf-8">{pre}</head><body><p>x</p></body></html>'
    )


def test_sdk_bridges_to_android_then_ios_and_escapes_script_end():
    sdk = artifact_page.sdk()
    assert "window.GacaBridge.call(id, method, JSON.stringify(args))" in sdk
    assert "window.webkit.messageHandlers.gaca" in sdk and "postMessage({ id: id, method: method, args: args })" in sdk
    assert "new Error('no app bridge')" in sdk
    built = artifact_page.build("<p>", "a1", "T")
    script = re.search(r"<script>(/\* GACA.*)</script></head>", built, re.S).group(1)
    assert "</script" not in script


def test_page_is_owner_scoped(http):
    aid = make(http)
    _current["id"] = "u2"
    assert http.get(f"/api/artifacts/{aid}/page").status_code == 404


# ── calls: same answer as the direct endpoint ───────────────────────────────

def test_reads_match_their_endpoints(http):
    aid = make(http, state={"n": 1}, collections={"items": {"milk": {"text": "Milk"}}})
    base = f"/api/artifacts/{aid}"
    assert call(http, aid, "data").json() == http.get(f"{base}/data").json()
    rev = http.get(f"{base}/data").json()["rev"]
    assert call(http, aid, "data", since_rev=rev).json() == http.get(f"{base}/data", params={"since_rev": rev}).json()
    assert call(http, aid, "data", since_rev=None).json() == http.get(f"{base}/data").json()
    assert call(http, aid, "state.get").json() == http.get(f"{base}/state").json()
    assert call(http, aid, "doc.list", collection="items").json() == http.get(f"{base}/collections/items").json()


def test_writes_match_their_endpoints(http):
    aid = make(http, state={"n": 1})
    base = f"/api/artifacts/{aid}"

    r = call(http, aid, "state.set", state={"n": 2}, expected_version=1, junk="ignored")
    assert r.status_code == 200 and r.json() == http.get(f"{base}/state").json()
    r = call(http, aid, "state.patch", patch={"m": 3}, expected_version=None)
    assert r.json() == http.get(f"{base}/state").json() and r.json()["state"] == {"n": 2, "m": 3}

    added = call(http, aid, "doc.add", collection="items", data={"text": "Milk"}, id="milk").json()
    assert added == http.get(f"{base}/collections/items/milk").json()
    put = call(http, aid, "doc.set", collection="items", id="eggs", data={"text": "Eggs"}).json()
    assert put == http.get(f"{base}/collections/items/eggs").json()
    patched = call(http, aid, "doc.patch", collection="items", id="eggs", patch={"qty": 12}, expected_version=1).json()
    assert patched == http.get(f"{base}/collections/items/eggs").json() and patched["version"] == 2

    batch = call(http, aid, "batch", ops=[{"op": "set", "collection": "items", "id": "jam", "data": {}}]).json()
    assert batch == {"applied": 1, "rev": http.get(f"{base}/data").json()["rev"]}

    emitted = call(http, aid, "emit", name="checked_out", payload={"total": 3}).json()
    assert emitted == http.get(f"{base}/events").json()["events"][-1]

    r = call(http, aid, "doc.remove", collection="items", id="jam")
    assert r.status_code == 200 and r.json() is None
    assert http.get(f"{base}/collections/items/jam").status_code == 404
    assert call(http, aid, "doc.clear", collection="items").json() is None
    assert http.get(f"{base}/collections/items").json()["docs"] == []


def test_errors_match_their_endpoints(http):
    aid = make(http, state={"n": 1})
    base = f"/api/artifacts/{aid}"

    def same(via_call, direct):
        assert (via_call.status_code, via_call.json()) == (direct.status_code, direct.json())

    # stale expected_version -> 409
    same(call(http, aid, "state.set", state={}, expected_version=7),
         http.put(f"{base}/state", json={"state": {}, "expected_version": 7}))
    # schema miss -> FastAPI's own 422 body
    same(call(http, aid, "state.set", state=[1]), http.put(f"{base}/state", json={"state": [1]}))
    same(call(http, aid, "emit", name=""), http.post(f"{base}/events", json={"name": ""}))
    # missing doc -> 404
    same(call(http, aid, "doc.patch", collection="items", id="nope", patch={}),
         http.patch(f"{base}/collections/items/nope", json={"patch": {}}))
    # someone else's artifact -> 404, same as any route
    _current["id"] = "u2"
    same(call(http, aid, "state.get"), http.get(f"{base}/state"))
    _current["id"] = "u1"

    bad_name = call(http, aid, "doc.list", collection="Bad Name")
    assert (bad_name.status_code, bad_name.json()) == (422, {"detail": "bad collection name"})
    bad_id = call(http, aid, "doc.remove", collection="items", id="../x")
    assert (bad_id.status_code, bad_id.json()) == (422, {"detail": "bad document id"})
    assert call(http, aid, "data", since_rev=-1).status_code == 422


@pytest.mark.parametrize("method", ["nope", "ask", "openUrl", "toast"])
def test_unknown_and_ui_methods_are_400(http, method):
    aid = make(http)
    r = call(http, aid, method, text="x")
    assert (r.status_code, r.json()) == (400, {"detail": f"unknown method {method}"})
