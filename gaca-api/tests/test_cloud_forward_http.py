"""The forwarding layer: a cloud-homed gacabit answers every /api/artifacts/{id}/... route by calling
the (fake) cloud with this server's token. Dumb pass-through: statuses, bodies and ETags come back
as the cloud sent them; the cloud being out of reach is a clear 502."""
import json
import logging
import sqlite3

import pytest

from tests.cloud_support import (  # noqa: F401  (fixtures)
    SERVER_TOKEN, cloud, gacabit, http, link, mint,
)

A = "/api/artifacts/g1"


@pytest.fixture
def linked(http, cloud):
    """A linked server that has seen one shared gacabit, g1 (role editor, shared by alice)."""
    link(http, cloud)
    cloud.on("GET", "/gacabits", json={"gacabits": [gacabit()]})
    got = http.get("/api/artifacts", headers=http.session).json()
    assert [a["id"] for a in got["artifacts"]] == ["g1"]
    cloud.requests.clear()
    return http


def hdr(http, **extra):
    return {**http.session, **extra}


# ── what gets forwarded, per route group ─────────────────────────────────────
ROUTES = [
    # (method, local suffix, request json, cloud path, cloud status, cloud json)
    ("GET", "/data", None, "/gacabits/g1/data", 200, {"rev": 4, "changed": True, "state": {}, "collections": {}}),
    ("GET", "/state", None, "/gacabits/g1/state", 200, {"state": {"a": 1}, "version": 2, "rev": 4}),
    ("PUT", "/state", {"state": {"a": 2}, "expected_version": 2}, "/gacabits/g1/state", 200, {"state": {"a": 2}, "version": 3, "rev": 5}),
    ("PATCH", "/state", {"patch": {"a": None}}, "/gacabits/g1/state", 200, {"state": {}, "version": 4, "rev": 6}),
    ("POST", "/batch", {"ops": [{"op": "set", "collection": "items", "id": "x", "data": {"t": 1}}]}, "/gacabits/g1/batch", 200, {"applied": 1, "rev": 7}),
    ("GET", "/collections/items", None, "/gacabits/g1/collections/items", 200, {"collection": "items", "rev": 1, "docs": []}),
    ("POST", "/collections/items", {"data": {"t": 1}}, "/gacabits/g1/collections/items", 201, {"id": "d1", "version": 1, "data": {"t": 1}}),
    ("DELETE", "/collections/items", None, "/gacabits/g1/collections/items", 204, None),
    ("GET", "/collections/items/d1", None, "/gacabits/g1/collections/items/d1", 200, {"id": "d1", "version": 1, "data": {}}),
    ("PUT", "/collections/items/d1", {"data": {"t": 2}, "expected_version": 1}, "/gacabits/g1/collections/items/d1", 200, {"id": "d1", "version": 2, "data": {"t": 2}}),
    ("PATCH", "/collections/items/d1", {"patch": {"t": 3}}, "/gacabits/g1/collections/items/d1", 200, {"id": "d1", "version": 3, "data": {"t": 3}}),
    ("DELETE", "/collections/items/d1", None, "/gacabits/g1/collections/items/d1", 204, None),
    ("POST", "/events", {"name": "picked", "payload": {"day": "Fri"}}, "/gacabits/g1/events", 201, {"seq": 1, "name": "picked"}),
    ("GET", "/events?after=3&unhandled=true&limit=10", None, "/gacabits/g1/events", 200, {"events": []}),
    ("POST", "/events/handled", {"upto": 3}, "/gacabits/g1/events/handled", 200, {"handled": 3}),
    ("GET", "/versions", None, "/gacabits/g1/versions", 200, {"current": 1, "versions": []}),
    ("POST", "/opened", None, "/gacabits/g1/opened", 204, None),
]


@pytest.mark.parametrize("method,suffix,body,cloud_path,status,answer", ROUTES)
def test_every_data_route_is_forwarded_unchanged(linked, cloud, method, suffix, body, cloud_path, status, answer):
    cloud.on(method, cloud_path, status=status, json=answer)
    r = linked.request(method, A + suffix, headers=hdr(linked), json=body)
    assert r.status_code == status
    if answer is not None:
        assert r.json() == answer
    sent = cloud.seen(method, cloud_path)
    assert len(sent) == 1
    # the cloud was called with the SERVER token, never the app's own bearer
    assert sent[0].headers["authorization"] == f"Bearer {SERVER_TOKEN}"
    assert linked.session["Authorization"].split()[1] not in str(sent[0].headers)
    if body is not None:
        assert json.loads(sent[0].content) == body  # the body goes through untouched, not re-validated here
    assert SERVER_TOKEN not in r.text and "cloud.test" not in r.text


def test_query_parameters_are_forwarded_only_when_the_route_knows_them(linked, cloud):
    cloud.on("GET", "/gacabits/g1/events", json={"events": []})
    linked.get(A + "/events?after=3&unhandled=true&limit=10&evil=1", headers=hdr(linked))
    q = dict(cloud.seen("GET", "/gacabits/g1/events")[0].url.params)
    assert q == {"after": "3", "unhandled": "true", "limit": "10"}


def test_html_is_forwarded_with_the_local_hardening_headers(linked, cloud):
    cloud.on("GET", "/gacabits/g1/html", content=b"<h1>hi</h1>", headers={"content-type": "text/html"})
    r = linked.get(A + "/html?version=2", headers=hdr(linked))
    assert r.text == "<h1>hi</h1>" and "sandbox" in r.headers["content-security-policy"]
    assert dict(cloud.seen("GET", "/gacabits/g1/html")[0].url.params) == {"version": "2"}


def test_metadata_routes_keep_cloud_fields_and_add_the_local_ones(linked, cloud):
    cloud.on("GET", "/gacabits/g1", json=gacabit(rev=9, extra_cloud_field="kept"))
    g = linked.get(A, headers=hdr(linked)).json()
    assert g["extra_cloud_field"] == "kept" and g["rev"] == 9 and g["role"] == "editor"
    assert g["home"] == "cloud" and g["shared_by"] == "alice" and g["link"] == "gaca://artifact/g1"
    assert g["crew"] is None and g["archived"] is False  # the keys an app reads from an artifact


def test_patch_and_restore_are_forwarded(linked, cloud):
    cloud.on("PATCH", "/gacabits/g1", json=gacabit(title="New", role="owner"))
    cloud.on("POST", "/gacabits/g1/restore", json=gacabit(version=3))
    r = linked.patch(A, headers=hdr(linked), json={"title": "New", "html": "<p>x</p>", "note": "n"})
    assert r.json()["title"] == "New"
    assert json.loads(cloud.seen("PATCH", "/gacabits/g1")[0].content) == {"title": "New", "html": "<p>x</p>", "note": "n"}
    assert linked.post(A + "/restore", headers=hdr(linked), json={"version": 1}).json()["version"] == 3
    # the cached title follows what the cloud said
    stored = sqlite3.connect(linked.db)
    assert stored.execute("SELECT title, role FROM cloud_pointers").fetchone() == ("Shared list", "editor")  # the last answer (restore) wins
    stored.close()


# ── error mapping ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("status,code", [
    (403, "forbidden_role"), (404, "not_found"), (409, "conflict"), (422, "schema_violation"),
    (423, "read_only"), (429, "rate_limited"), (402, "billing_suspended"), (410, "invite_expired"),
    (413, "gacabit_size_exceeded"),
])
def test_cloud_decisions_come_back_faithfully(linked, cloud, status, code):
    body = {"code": code, "message": "because", "detail": {"required_role": "editor"}}
    cloud.on("PUT", "/gacabits/g1/state", status=status, json=body, headers={"retry-after": "7"})
    r = linked.put(A + "/state", headers=hdr(linked), json={"state": {}})
    assert r.status_code == status
    assert r.json()["code"] == code and r.json()["message"] == "because" and r.json()["detail"] == "because" and r.json()["info"] == {"required_role": "editor"}  # detail is always a string (8b)
    assert r.headers["retry-after"] == "7"


def test_error_bodies_gain_detail_when_the_cloud_sent_only_a_message(linked, cloud):
    cloud.on("GET", "/gacabits/g1/state", status=423, json={"code": "read_only", "message": "Read only for now"})
    r = linked.get(A + "/state", headers=hdr(linked))
    assert r.status_code == 423 and r.json() == {"code": "read_only", "message": "Read only for now", "detail": "Read only for now"}


@pytest.mark.parametrize("status", [500, 502, 504])
def test_cloud_server_errors_are_cloud_unavailable(linked, cloud, status):
    cloud.on("GET", A.replace("/api/artifacts", "/gacabits") + "/state", status=status, json={"oops": "internal https://cloud.test/x"})
    r = linked.get(A + "/state", headers=hdr(linked))
    assert r.status_code == 502 and r.json()["code"] == "cloud_unavailable"
    assert "cloud.test" not in r.text and "oops" not in r.text


def test_a_503_with_a_cloud_error_code_is_the_clouds_own_retry_answer(linked, cloud):
    cloud.on("GET", "/gacabits/g1/state", status=503, json={"code": "validation_unavailable", "message": "retry"})
    r = linked.get(A + "/state", headers=hdr(linked))
    assert r.status_code == 503 and r.json()["code"] == "validation_unavailable"


def test_cloud_down_and_slow_are_502_cloud_unavailable_with_a_clear_message(linked, cloud):
    for mode in ("down", "slow"):
        cloud.down, cloud.slow = mode == "down", mode == "slow"
        r = linked.get(A + "/state", headers=hdr(linked))
        assert r.status_code == 502 and r.json()["code"] == "cloud_unavailable"
        assert "cloud" in r.json()["message"].lower()
        assert SERVER_TOKEN not in r.text and "cloud.test" not in r.text and "boom" not in r.text


def test_cloud_rejecting_our_token_is_not_passed_on_as_401(linked, cloud):
    cloud.on("GET", "/gacabits/g1/state", status=401, json={"code": "unauthenticated", "message": "no"})
    r = linked.get(A + "/state", headers=hdr(linked))
    # a 401 here would tell the app it had been signed out of ITS server
    assert r.status_code == 502 and r.json()["code"] == "cloud_link_invalid"
    assert linked.get("/api/cloud/link", headers=hdr(linked)).json()["state"] == "invalid"
    again = linked.get(A + "/state", headers=hdr(linked))
    assert again.json()["code"] == "cloud_link_invalid" and len(cloud.seen("GET", "/gacabits/g1/state")) == 1


def test_redirects_from_the_cloud_are_never_followed(linked, cloud):
    cloud.redirect_to = "https://evil.example/x"
    r = linked.get(A + "/state", headers=hdr(linked))
    assert r.status_code == 502 and {q.url.host for q in cloud.requests} == {"cloud.test"}


# ── etags, request ids, long-poll ────────────────────────────────────────────
def test_etags_and_conditional_requests_pass_through(linked, cloud):
    cloud.on("GET", "/gacabits/g1/state", json={"state": {}, "version": 1, "rev": 1}, headers={"etag": '"v1"'})
    r = linked.get(A + "/state", headers=hdr(linked, **{"If-None-Match": '"v1"'}))
    assert r.headers["etag"] == '"v1"'
    assert cloud.seen("GET", "/gacabits/g1/state")[0].headers["if-none-match"] == '"v1"'
    cloud.on("GET", "/gacabits/g1/state", status=304, headers={"etag": '"v1"'})
    r = linked.get(A + "/state", headers=hdr(linked, **{"If-None-Match": '"v1"'}))
    assert r.status_code == 304 and r.content == b"" and r.headers["etag"] == '"v1"'


def test_if_match_is_forwarded_on_writes(linked, cloud):
    cloud.on("PUT", "/gacabits/g1/state", status=412, json={"code": "conflict", "message": "stale"})
    r = linked.put(A + "/state", headers=hdr(linked, **{"If-Match": '"old"'}), json={"state": {}})
    assert r.status_code == 412
    assert cloud.seen("PUT", "/gacabits/g1/state")[0].headers["if-match"] == '"old"'


def test_request_id_is_propagated_both_ways_and_made_up_when_missing(linked, cloud):
    cloud.on("GET", "/gacabits/g1/state", json={"state": {}, "version": 1, "rev": 1})
    r = linked.get(A + "/state", headers=hdr(linked, **{"X-Request-Id": "req-123"}))
    assert cloud.seen("GET", "/gacabits/g1/state")[-1].headers["x-request-id"] == "req-123"
    assert r.headers["x-request-id"] == "req-123"
    linked.get(A + "/state", headers=hdr(linked, **{"X-Request-Id": "bad id with spaces\n"}))
    made = cloud.seen("GET", "/gacabits/g1/state")[-1].headers["x-request-id"]
    assert made and " " not in made and made != "bad id with spaces"
    linked.get(A + "/state", headers=hdr(linked))
    assert cloud.seen("GET", "/gacabits/g1/state")[-1].headers["x-request-id"]


def test_long_poll_gets_a_read_timeout_longer_than_the_wait_and_wait_is_capped(linked, cloud):
    cloud.on("GET", "/gacabits/g1/data", json={"rev": 4, "changed": False})
    linked.get(A + "/data?since_rev=4&wait=8", headers=hdr(linked))
    req = cloud.seen("GET", "/gacabits/g1/data")[-1]
    assert dict(req.url.params) == {"since_rev": "4", "wait": "8"}
    assert cloud.timeouts[-1]["read"] >= 18  # 8 s wait + margin, and never below the 15 s floor
    linked.get(A + "/data?since_rev=4&wait=9999", headers=hdr(linked))
    assert dict(cloud.seen("GET", "/gacabits/g1/data")[-1].url.params)["wait"] == "25"
    assert cloud.timeouts[-1]["read"] <= 40
    linked.get(A + "/state", headers=hdr(linked))
    assert cloud.timeouts[-1]["read"] >= 15  # an ordinary call also waits longer than the cloud's long-poll


def test_long_poll_that_times_out_is_a_502_not_a_hang(linked, cloud):
    cloud.slow = True
    r = linked.get(A + "/data?since_rev=4&wait=8", headers=hdr(linked))
    assert r.status_code == 502 and r.json()["code"] == "cloud_unavailable"


def test_bad_wait_is_refused_locally(linked, cloud):
    assert linked.get(A + "/data?wait=abc", headers=hdr(linked)).status_code == 422
    assert cloud.requests == []


def test_hermes_with_an_artifacts_token_uses_the_same_forwarded_routes(linked, cloud):
    hermes = mint(linked, scopes=("artifacts", "cloud"))
    cloud.on("POST", "/gacabits/g1/collections/items", status=201, json={"id": "d1", "version": 1, "data": {}})
    r = linked.post(A + "/collections/items", headers=hermes, json={"data": {"t": 1}})
    assert r.status_code == 201
    sent = cloud.seen("POST", "/gacabits/g1/collections/items")[0]
    assert sent.headers["authorization"] == f"Bearer {SERVER_TOKEN}"
    assert hermes["Authorization"].split()[1] not in str(sent.headers)
    assert linked.post(A + "/collections/items", json={"data": {}}).status_code == 401


# ── /page and /call (what the WebView uses) ──────────────────────────────────
def page_routes(cloud, version=1, html="<h1>Shared</h1>"):
    cloud.on("GET", "/gacabits/g1", json=gacabit(version=version))
    cloud.on("GET", "/gacabits/g1/html", content=html.encode(), headers={"content-type": "text/html"})


def test_page_is_the_cloud_html_wrapped_locally_with_the_local_contract(linked, cloud):
    page_routes(cloud)
    r = linked.get(A + "/page", headers=hdr(linked))
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "<h1>Shared</h1>" in r.text and "window.__GACA_INIT" in r.text and "window.app" in r.text
    assert '"id":"g1"' in r.text and "Shared list" in r.text  # the local wrapper, not the cloud's
    assert "default-src 'none'" in r.headers["content-security-policy"] and r.headers["cache-control"] == "no-store"
    assert SERVER_TOKEN not in r.text and "cloud.test" not in r.text


def test_page_html_is_cached_by_version_and_refetched_on_a_new_one(linked, cloud):
    page_routes(cloud)
    linked.get(A + "/page", headers=hdr(linked))
    linked.get(A + "/page", headers=hdr(linked))
    assert len(cloud.seen("GET", "/gacabits/g1/html")) == 1
    page_routes(cloud, version=2, html="<h1>v2</h1>")
    assert "<h1>v2</h1>" in linked.get(A + "/page", headers=hdr(linked)).text
    assert len(cloud.seen("GET", "/gacabits/g1/html")) == 2


def test_page_still_opens_from_the_cache_when_the_cloud_is_down(linked, cloud):
    page_routes(cloud)
    linked.get(A + "/page", headers=hdr(linked))
    cloud.down = True
    r = linked.get(A + "/page", headers=hdr(linked))
    assert r.status_code == 200 and "<h1>Shared</h1>" in r.text
    r = linked.post(A + "/call", headers=hdr(linked), json={"method": "data", "args": {}})
    assert r.status_code == 502  # but its data calls fail, so the page can say so


def test_page_with_no_cache_and_the_cloud_down_is_502(linked, cloud):
    cloud.down = True
    r = linked.get(A + "/page", headers=hdr(linked))
    assert r.status_code == 502 and r.json()["code"] == "cloud_unavailable"


def test_page_for_a_gacabit_we_lost_access_to_drops_the_pointer(linked, cloud):
    cloud.on("GET", "/gacabits/g1", status=404, json={"code": "not_found", "message": "gone"})
    assert linked.get(A + "/page", headers=hdr(linked)).status_code == 404
    cloud.on("GET", "/gacabits", json={"gacabits": []})
    assert linked.get("/api/artifacts", headers=hdr(linked)).json()["artifacts"] == []
    assert linked.get(A, headers=hdr(linked)).status_code == 404  # now just an unknown local id


CALLS = [
    ("data", {"since_rev": 4}, "GET", "/gacabits/g1/data", None, 200, {"rev": 4, "changed": False}),
    ("data", {}, "GET", "/gacabits/g1/data", None, 200, {"rev": 1, "changed": True}),
    ("state.get", {}, "GET", "/gacabits/g1/state", None, 200, {"state": {}, "version": 1, "rev": 1}),
    ("state.set", {"state": {"a": 1}, "expected_version": 1}, "PUT", "/gacabits/g1/state", {"state": {"a": 1}, "expected_version": 1}, 200, {"state": {"a": 1}, "version": 2, "rev": 2}),
    ("state.patch", {"patch": {"a": None}}, "PATCH", "/gacabits/g1/state", {"patch": {"a": None}}, 200, {"state": {}, "version": 3, "rev": 3}),
    ("doc.list", {"collection": "items"}, "GET", "/gacabits/g1/collections/items", None, 200, {"collection": "items", "rev": 1, "docs": []}),
    ("doc.add", {"collection": "items", "data": {"t": 1}, "id": "d1"}, "POST", "/gacabits/g1/collections/items", {"data": {"t": 1}, "id": "d1"}, 201, {"id": "d1", "version": 1, "data": {"t": 1}}),
    ("doc.set", {"collection": "items", "id": "d1", "data": {"t": 2}}, "PUT", "/gacabits/g1/collections/items/d1", {"data": {"t": 2}}, 200, {"id": "d1", "version": 2, "data": {"t": 2}}),
    ("doc.patch", {"collection": "items", "id": "d1", "patch": {"t": 3}}, "PATCH", "/gacabits/g1/collections/items/d1", {"patch": {"t": 3}}, 200, {"id": "d1", "version": 3, "data": {"t": 3}}),
    ("batch", {"ops": [{"op": "clear", "collection": "items"}]}, "POST", "/gacabits/g1/batch", {"ops": [{"op": "clear", "collection": "items"}]}, 200, {"applied": 1, "rev": 5}),
    ("emit", {"name": "picked", "payload": {"d": 1}}, "POST", "/gacabits/g1/events", {"name": "picked", "payload": {"d": 1}}, 201, {"seq": 1, "name": "picked"}),
]


@pytest.mark.parametrize("method,args,http_method,cloud_path,payload,status,answer", CALLS)
def test_every_sdk_call_maps_to_its_cloud_route(linked, cloud, method, args, http_method, cloud_path, payload, status, answer):
    cloud.on(http_method, cloud_path, status=status, json=answer)
    r = linked.post(A + "/call", headers=hdr(linked), json={"method": method, "args": args})
    assert r.status_code == 200 and r.json() == answer  # the local /call answers 200 with the endpoint's JSON
    sent = cloud.seen(http_method, cloud_path)[0]
    assert (json.loads(sent.content) if sent.content else None) == payload


@pytest.mark.parametrize("method,args,http_method,cloud_path", [
    ("doc.remove", {"collection": "items", "id": "d1"}, "DELETE", "/gacabits/g1/collections/items/d1"),
    ("doc.clear", {"collection": "items"}, "DELETE", "/gacabits/g1/collections/items"),
])
def test_calls_the_cloud_answers_204_to_come_back_as_null(linked, cloud, method, args, http_method, cloud_path):
    cloud.on(http_method, cloud_path, status=204)
    r = linked.post(A + "/call", headers=hdr(linked), json={"method": method, "args": args})
    assert r.status_code == 200 and r.json() is None


def test_call_keeps_the_local_error_contract_for_ui_methods_and_bad_names(linked, cloud):
    for method in ("ask", "openUrl", "toast", "nope"):
        assert linked.post(A + "/call", headers=hdr(linked), json={"method": method, "args": {}}).status_code == 400
    bad = linked.post(A + "/call", headers=hdr(linked), json={"method": "doc.list", "args": {"collection": "../x"}})
    assert bad.status_code == 422
    bad = linked.post(A + "/call", headers=hdr(linked), json={"method": "doc.set", "args": {"collection": "a", "id": "../x", "data": {}}})
    assert bad.status_code == 422
    assert linked.post(A + "/call", headers=hdr(linked), json={"method": "data", "args": {"since_rev": -1}}).status_code == 422
    assert cloud.requests == []  # none of those reached the cloud


def test_call_errors_pass_through_with_the_cloud_status(linked, cloud):
    cloud.on("PUT", "/gacabits/g1/state", status=409, json={"code": "conflict", "message": "stale"})
    r = linked.post(A + "/call", headers=hdr(linked), json={"method": "state.set", "args": {"state": {}, "expected_version": 1}})
    assert r.status_code == 409 and r.json()["code"] == "conflict"
    cloud.on("POST", "/gacabits/g1/events", status=403, json={"code": "forbidden_role", "message": "viewer"})
    r = linked.post(A + "/call", headers=hdr(linked), json={"method": "emit", "args": {"name": "x"}})
    assert r.status_code == 403


def test_call_data_long_poll_passes_wait_with_a_long_enough_timeout(linked, cloud):
    cloud.on("GET", "/gacabits/g1/data", json={"rev": 4, "changed": False})
    linked.post(A + "/call", headers=hdr(linked), json={"method": "data", "args": {"since_rev": 4, "wait": 8}})
    assert dict(cloud.seen("GET", "/gacabits/g1/data")[0].url.params) == {"since_rev": "4", "wait": "8"}
    assert cloud.timeouts[-1]["read"] >= 15


# ── delete means leave ───────────────────────────────────────────────────────
def test_delete_leaves_the_gacabit_and_drops_the_pointer(linked, cloud):
    cloud.on("POST", "/gacabits/g1/leave", status=204)
    assert linked.delete(A, headers=hdr(linked)).status_code == 204
    assert cloud.seen("DELETE") == []  # never a real delete of the cloud's copy
    assert len(cloud.seen("POST", "/gacabits/g1/leave")) == 1
    assert linked.get(A, headers=hdr(linked)).status_code == 404  # no longer a pointer: just an unknown local id
    cloud.on("GET", "/gacabits", json={"gacabits": []})  # (and the cloud no longer lists it for us)
    assert linked.get("/api/artifacts", headers=hdr(linked)).json()["artifacts"] == []


def test_the_owner_cannot_delete_by_leaving_and_keeps_the_pointer(linked, cloud):
    cloud.on("POST", "/gacabits/g1/leave", status=409, json={"code": "owner_cannot_leave", "message": "owner"})
    r = linked.delete(A, headers=hdr(linked))
    assert r.status_code == 409 and r.json()["code"] == "owner_cannot_leave"
    cloud.on("GET", "/gacabits/g1", json=gacabit(role="owner"))
    assert linked.get(A, headers=hdr(linked)).status_code == 200


# ── things that must stay local ──────────────────────────────────────────────
def test_local_artifacts_are_untouched_by_a_link(linked, cloud):
    made = linked.post("/api/artifacts", headers=hdr(linked), json={"title": "Mine", "html": "<p>mine</p>", "state": {"a": 1}})
    aid = made.json()["id"]
    cloud.requests.clear()
    assert linked.get(f"/api/artifacts/{aid}/state", headers=hdr(linked)).json()["state"] == {"a": 1}
    assert "<p>mine</p>" in linked.get(f"/api/artifacts/{aid}/page", headers=hdr(linked)).text
    assert cloud.requests == []


def test_unknown_routes_for_a_pointer_fall_through_to_the_local_answer(linked, cloud):
    assert linked.put(A, headers=hdr(linked), json={}).status_code == 405
    assert linked.get(A + "/collections/Bad_Name", headers=hdr(linked)).status_code == 422
    assert cloud.requests == []


def test_a_path_cannot_steer_the_forwarded_call(linked, cloud):
    r = linked.get(A + "/collections/items/..%2F..%2Fme", headers=hdr(linked))
    assert r.status_code in (404, 405, 422) and cloud.requests == []


def test_nothing_in_any_response_or_log_holds_the_token(linked, cloud, caplog):
    caplog.set_level(logging.DEBUG)
    cloud.on("GET", "/gacabits/g1/state", json={"state": {}, "version": 1, "rev": 1})
    page_routes(cloud)
    seen = [
        linked.get(A + "/state", headers=hdr(linked)), linked.get(A + "/page", headers=hdr(linked)),
        linked.get(A, headers=hdr(linked)), linked.get("/api/artifacts", headers=hdr(linked)),
        linked.get("/api/cloud/link", headers=hdr(linked)),
    ]
    cloud.down = True
    seen += [linked.get(A + "/state", headers=hdr(linked)), linked.get("/api/artifacts", headers=hdr(linked))]
    for r in seen:
        assert SERVER_TOKEN not in r.text and "gsrv_" not in r.text and "cloud.test" not in r.text
        assert SERVER_TOKEN not in str(r.headers)
    assert SERVER_TOKEN not in caplog.text and "cloud.test" not in caplog.text


# ── the merged list ──────────────────────────────────────────────────────────
def test_list_without_a_link_has_every_old_field_and_the_new_ones(http):
    made = http.post("/api/artifacts", headers=http.session, json={"title": "Mine", "html": "<p>x</p>"}).json()
    got = http.get("/api/artifacts", headers=http.session).json()
    assert set(got) == {"artifacts", "stale"} and got["stale"] is False
    item = got["artifacts"][0]
    for key, value in made.items():  # everything the old list item had is still there, with the same value
        assert item[key] == value, key
    assert (item["home"], item["role"], item["shared_by"], item["member_count"]) == ("local", "owner", None, 1)


def test_list_merges_cloud_pointers_with_home_role_shared_by_member_count(linked, cloud, monkeypatch):
    from gaca_api.config import settings

    monkeypatch.setattr(settings, "cloud_list_ttl_seconds", 0)
    cloud.on("GET", "/gacabits", json={"gacabits": [gacabit(), gacabit("g2", "Mine in cloud", role="owner", owner="david")]})
    linked.post("/api/artifacts", headers=hdr(linked), json={"title": "Local one", "html": "<p>x</p>"})
    got = linked.get("/api/artifacts", headers=hdr(linked)).json()
    by = {a["title"]: a for a in got["artifacts"]}
    assert set(by) == {"Local one", "Shared list", "Mine in cloud"} and got["stale"] is False
    assert by["Local one"]["home"] == "local"
    s = by["Shared list"]
    assert (s["home"], s["role"], s["shared_by"], s["member_count"]) == ("cloud", "editor", "alice", 2)
    assert s["link"] == "gaca://artifact/g1" and s["id"] == "g1" and s["version"] == 1 and s["rev"] == 3
    o = by["Mine in cloud"]
    assert (o["home"], o["role"], o["shared_by"]) == ("cloud", "owner", None)
    assert dict(cloud.seen("GET", "/gacabits")[-1].url.params)["scope"] == "all"


def test_list_is_cached_briefly_then_asks_again(linked, cloud, monkeypatch):
    from gaca_api.config import settings

    cloud.on("GET", "/gacabits", json={"gacabits": [gacabit()]})
    linked.get("/api/artifacts", headers=hdr(linked))
    linked.get("/api/artifacts", headers=hdr(linked))
    assert len(cloud.seen("GET", "/gacabits")) == 0  # the fixture already synced inside the TTL
    monkeypatch.setattr(settings, "cloud_list_ttl_seconds", 0)
    linked.get("/api/artifacts", headers=hdr(linked))
    assert len(cloud.seen("GET", "/gacabits")) == 1


def test_stale_while_error_serves_the_last_known_rows_with_a_marker(linked, cloud, monkeypatch):
    from gaca_api.config import settings

    monkeypatch.setattr(settings, "cloud_list_ttl_seconds", 0)
    cloud.down = True
    got = linked.get("/api/artifacts", headers=hdr(linked)).json()
    assert got["stale"] is True and [a["id"] for a in got["artifacts"]] == ["g1"]
    assert got["artifacts"][0]["home"] == "cloud" and got["artifacts"][0]["shared_by"] == "alice"
    seen = len(cloud.requests)
    assert linked.get("/api/artifacts", headers=hdr(linked)).json()["stale"] is True  # backing off: no new attempt
    assert len(cloud.requests) == seen
    cloud.down = False
    cloud.on("GET", "/gacabits", json={"gacabits": [gacabit()]})
    monkeypatch.setattr(settings, "cloud_list_backoff_seconds", 0)
    assert linked.get("/api/artifacts", headers=hdr(linked)).json()["stale"] is False


def test_a_cloud_error_answer_also_serves_stale_and_does_not_lose_pointers(linked, cloud, monkeypatch):
    from gaca_api.config import settings

    monkeypatch.setattr(settings, "cloud_list_ttl_seconds", 0)
    cloud.on("GET", "/gacabits", status=500, json={"x": 1})
    got = linked.get("/api/artifacts", headers=hdr(linked)).json()
    assert got["stale"] is True and len(got["artifacts"]) == 1


def test_a_gacabit_that_left_the_cloud_list_leaves_this_one(linked, cloud, monkeypatch):
    from gaca_api.config import settings

    monkeypatch.setattr(settings, "cloud_list_ttl_seconds", 0)
    cloud.on("GET", "/gacabits", json={"gacabits": []})
    assert linked.get("/api/artifacts", headers=hdr(linked)).json()["artifacts"] == []


def test_list_filters_archived_q_and_session_alert(linked, cloud, monkeypatch):
    from gaca_api.config import settings

    monkeypatch.setattr(settings, "cloud_list_ttl_seconds", 0)
    cloud.on("GET", "/gacabits", json={"gacabits": [gacabit(), gacabit("g2", "Old", archived=True)]})
    assert [a["id"] for a in linked.get("/api/artifacts", headers=hdr(linked)).json()["artifacts"]] == ["g1"]
    both = linked.get("/api/artifacts?include_archived=true", headers=hdr(linked)).json()["artifacts"]
    assert {a["id"] for a in both} == {"g1", "g2"}
    assert linked.get("/api/artifacts?q=shared", headers=hdr(linked)).json()["artifacts"][0]["id"] == "g1"
    assert linked.get("/api/artifacts?q=zzz", headers=hdr(linked)).json()["artifacts"] == []
    assert linked.get("/api/artifacts?session_id=s1", headers=hdr(linked)).json()["artifacts"] == []
    assert linked.get("/api/artifacts?limit=1", headers=hdr(linked)).json()["artifacts"].__len__() == 1


def test_a_local_artifact_with_the_same_id_is_never_hidden_behind_a_pointer(linked, cloud, monkeypatch):
    from gaca_api.config import settings

    local = linked.post("/api/artifacts", headers=hdr(linked), json={"title": "Local twin", "html": "<p>x</p>"}).json()
    monkeypatch.setattr(settings, "cloud_list_ttl_seconds", 0)
    cloud.on("GET", "/gacabits", json={"gacabits": [gacabit(local["id"], "Cloud twin")]})
    got = linked.get("/api/artifacts", headers=hdr(linked)).json()["artifacts"]
    assert [(a["title"], a["home"]) for a in got] == [("Local twin", "local")]


def test_ids_that_could_steer_a_url_never_become_pointers(linked, cloud, monkeypatch):
    from gaca_api.config import settings

    monkeypatch.setattr(settings, "cloud_list_ttl_seconds", 0)
    cloud.on("GET", "/gacabits", json={"gacabits": [gacabit("../me", "Evil"), gacabit("a/b", "Evil2"), gacabit("ok_1", "Fine")]})
    assert [a["id"] for a in linked.get("/api/artifacts", headers=hdr(linked)).json()["artifacts"]] == ["ok_1"]


def test_list_pages_through_the_cloud_listing(linked, cloud, monkeypatch):
    from gaca_api.config import settings

    monkeypatch.setattr(settings, "cloud_list_ttl_seconds", 0)

    def pages(request):
        import httpx

        if request.url.params.get("cursor") == "c2":
            return httpx.Response(200, json={"gacabits": [gacabit("g3", "Third")], "next_cursor": None})
        return httpx.Response(200, json={"gacabits": [gacabit()], "next_cursor": "c2"})

    cloud.on_fn("GET", "/gacabits", pages)
    assert {a["id"] for a in linked.get("/api/artifacts", headers=hdr(linked)).json()["artifacts"]} == {"g1", "g3"}


def test_ordering_keeps_the_local_rule_over_local_and_cloud_rows(linked, cloud, monkeypatch):
    from gaca_api.config import settings

    monkeypatch.setattr(settings, "cloud_list_ttl_seconds", 0)
    cloud.on("GET", "/gacabits", json={"gacabits": [gacabit("g1", "A", updated_at="2026-11-02T10:00:00Z"),
                                                    gacabit("g2", "B", updated_at="2026-11-04T10:00:00Z")]})
    local = linked.post("/api/artifacts", headers=hdr(linked), json={"title": "Local", "html": "<p>x</p>"}).json()
    ids = [a["id"] for a in linked.get("/api/artifacts", headers=hdr(linked)).json()["artifacts"]]
    assert ids[0] == local["id"]  # newest of all (just made); then g2 (Nov 4), then g1 (Nov 2)
    assert ids[1:] == ["g2", "g1"]
    order = linked.put("/api/artifacts/order", headers=hdr(linked), json={"ids": ["g1", local["id"]]})
    assert [a["id"] for a in order.json()["artifacts"]] == ["g1", local["id"], "g2"]
    assert [a["id"] for a in linked.get("/api/artifacts", headers=hdr(linked)).json()["artifacts"]] == ["g1", local["id"], "g2"]
    assert linked.put("/api/artifacts/order", headers=hdr(linked), json={"ids": ["nope"]}).status_code == 404


# ── invites ──────────────────────────────────────────────────────────────────
def test_invite_preview_is_a_pass_through(linked, cloud):
    cloud.on("POST", "/invites/preview", json={"gacabit_title": "Trip", "role": "editor", "invited_by": {"id": "a", "handle": "alice"}, "already_member": False})
    r = linked.post("/api/cloud/invites/preview", headers=hdr(linked), json={"token": "tok123", "cloud_url": "https://evil.example"})
    assert r.json()["gacabit_title"] == "Trip"
    sent = cloud.seen("POST", "/invites/preview")[0]
    assert json.loads(sent.content) == {"token": "tok123"} and sent.url.host == "cloud.test"
    cloud.on("POST", "/invites/preview", status=410, json={"code": "invite_expired", "message": "old"})
    assert linked.post("/api/cloud/invites/preview", headers=hdr(linked), json={"token": "t"}).status_code == 410


def test_redeem_from_the_app_creates_the_pointer_so_it_shows_in_the_list(linked, cloud):
    cloud.on("POST", "/invites/redeem", json=gacabit("g7", "Trip", role="viewer", owner="bob"))
    r = linked.post("/api/cloud/invites/redeem", headers=hdr(linked), json={"token": "tok"})
    assert r.status_code == 200 and r.json()["id"] == "g7" and r.json()["home"] == "cloud" and r.json()["shared_by"] == "bob"
    cloud.on("GET", "/gacabits", json={"gacabits": [gacabit(), gacabit("g7", "Trip", role="viewer", owner="bob")]})
    got = {a["id"]: a for a in linked.get("/api/artifacts", headers=hdr(linked)).json()["artifacts"]}
    assert got["g7"]["role"] == "viewer"
    cloud.on("GET", "/gacabits/g7/state", json={"state": {}, "version": 1, "rev": 1})
    assert linked.get("/api/artifacts/g7/state", headers=hdr(linked)).status_code == 200


def test_redeem_makes_the_pointer_even_before_a_list_is_fetched(linked, cloud):
    cloud.on("POST", "/invites/redeem", json=gacabit("g8", "Fresh", role="editor"))
    linked.post("/api/cloud/invites/redeem", headers=hdr(linked), json={"token": "tok"})
    con = sqlite3.connect(linked.db)
    assert con.execute("SELECT id, cloud_id, role FROM cloud_pointers WHERE id = 'g8'").fetchone() == ("g8", "g8", "editor")
    con.close()


def test_agents_can_never_redeem_even_with_cloud_scope_and_a_user_confirmed_flag(linked, cloud):
    cloud.on("POST", "/invites/redeem", json=gacabit("g9", "Via hermes"))
    hermes = mint(linked, scopes=("artifacts", "cloud"))
    for body in ({"token": "tok"}, {"token": "tok", "user_confirmed": True}):
        assert linked.post("/api/cloud/invites/redeem", headers=hermes, json=body).status_code == 403
        assert linked.post("/api/cloud/invites/preview", headers=hermes, json=body).status_code == 403
    assert cloud.seen("POST", "/invites/redeem") == [] and cloud.seen("POST", "/invites/preview") == []
    assert linked.post("/api/cloud/invites/redeem", headers=hdr(linked), json={"token": "tok"}).status_code == 200


def test_redeem_errors_pass_through(linked, cloud):
    for status, code in ((404, "not_found"), (409, "already_member"), (410, "invite_expired")):
        cloud.on("POST", "/invites/redeem", status=status, json={"code": code, "message": "m"})
        r = linked.post("/api/cloud/invites/redeem", headers=hdr(linked), json={"token": "t"})
        assert r.status_code == status and r.json()["code"] == code


def test_cloud_routes_say_so_when_not_linked(http):
    r = http.post("/api/cloud/invites/preview", headers=http.session, json={"token": "t"})
    assert r.status_code == 409 and r.json()["code"] == "cloud_not_linked"
    assert http.get("/api/cloud/templates", headers=http.session).status_code == 409


# ── templates ────────────────────────────────────────────────────────────────
BUNDLE = {"bundle_version": 1, "kind": "template", "meta": {"title": "Habit tracker", "description": "Daily habits", "data_notes": "state: {}"},
          "html": "<h1>Habits</h1>", "schemas": {"habits": {"fields": {"name": {"type": "string", "required": True}}}}}


def test_template_list_and_detail_pass_through(linked, cloud):
    cloud.on("GET", "/templates", json={"templates": [{"id": "t1", "title": "Habit tracker"}], "next_cursor": None})
    cloud.on("GET", "/templates/t1", json={"id": "t1", "title": "Habit tracker"})
    r = linked.get("/api/cloud/templates?q=habit&sort=popular&evil=1", headers=hdr(linked))
    assert r.json()["templates"][0]["id"] == "t1"
    assert dict(cloud.seen("GET", "/templates")[0].url.params) == {"q": "habit", "sort": "popular"}
    assert linked.get("/api/cloud/templates/t1", headers=hdr(linked)).json()["id"] == "t1"
    cloud.on("GET", "/templates/t9", status=404, json={"code": "not_found", "message": "no"})
    assert linked.get("/api/cloud/templates/t9", headers=hdr(linked)).status_code == 404


def test_template_get_returns_the_bundle_for_a_server(linked, cloud):
    cloud.on("POST", "/templates/t1/get", json={"download_url": "https://s3.evil/x", "expires_in": 60, "bundle": BUNDLE})
    r = linked.post("/api/cloud/templates/t1/get", headers=hdr(linked), json={"target": "server_bundle"})
    assert r.status_code == 200 and r.json()["bundle"]["kind"] == "template"
    assert json.loads(cloud.seen("POST", "/templates/t1/get")[0].content) == {"target": "server_bundle"}
    # the cloud-billed target is not offered here
    assert linked.post("/api/cloud/templates/t1/get", headers=hdr(linked), json={"target": "cloud"}).status_code == 422


def test_template_install_creates_a_local_artifact_through_the_create_service(linked, cloud):
    cloud.on("POST", "/templates/t1/get", json={"download_url": "https://s3.evil/x", "expires_in": 60, "bundle": BUNDLE})
    r = linked.post("/api/cloud/templates/t1/install", headers=hdr(linked))
    assert r.status_code == 201
    made = r.json()
    assert made["title"] == "Habit tracker" and made["description"] == "Daily habits" and made["schemas"]["habits"]["fields"]["name"]["required"]
    assert linked.get(f"/api/artifacts/{made['id']}/html", headers=hdr(linked)).text == "<h1>Habits</h1>"
    assert {q.url.host for q in cloud.requests} == {"cloud.test"}  # the download_url was never fetched


def test_template_install_falls_back_to_the_clouds_own_bundle_route_never_a_url(linked, cloud):
    cloud.on("POST", "/templates/t1/get", json={"download_url": "https://s3.evil/x", "expires_in": 60})
    cloud.on("GET", "/templates/t1/bundle", json={"download_url": "x", "expires_in": 1, "bundle": BUNDLE})
    assert linked.post("/api/cloud/templates/t1/install", headers=hdr(linked)).status_code == 201
    assert {q.url.host for q in cloud.requests} == {"cloud.test"}


@pytest.mark.parametrize("bundle", [
    {**BUNDLE, "kind": "full"}, {**BUNDLE, "state": {"x": 1}}, {**BUNDLE, "html": ""}, {**BUNDLE, "meta": None},
    {**BUNDLE, "html": "x" * 1_100_000},
])
def test_a_bad_bundle_is_refused_with_bundle_invalid(linked, cloud, bundle):
    cloud.on("POST", "/templates/t1/get", json={"bundle": bundle})
    r = linked.post("/api/cloud/templates/t1/install", headers=hdr(linked))
    assert r.status_code == 422 and r.json()["code"] == "bundle_invalid"
    assert linked.get("/api/artifacts", headers=hdr(linked)).json()["artifacts"][0]["home"] == "cloud"  # nothing local was made
    assert len(linked.get("/api/artifacts", headers=hdr(linked)).json()["artifacts"]) == 1


def test_agents_cannot_get_or_install_templates_even_with_cloud_scope(linked, cloud):
    cloud.on("POST", "/templates/t1/get", json={"bundle": BUNDLE})
    hermes = mint(linked, scopes=("artifacts", "cloud"))
    assert linked.post("/api/cloud/templates/t1/install", headers=hermes).status_code == 403
    assert linked.post("/api/cloud/templates/t1/get", headers=hermes, json={}).status_code == 403
    assert cloud.seen("POST") == []


def test_template_routes_for_hermes_need_the_cloud_scope(linked, cloud):
    cloud.on("GET", "/templates", json={"templates": []})
    with_scope = mint(linked, name="a", scopes=("cloud",))
    without = mint(linked, name="b", scopes=("artifacts",))
    assert linked.get("/api/cloud/templates", headers=with_scope).status_code == 200
    assert linked.get("/api/cloud/templates", headers=without).status_code == 403
