"""Part 8b: the sharing pass-throughs (members, invite links, validations, publish, own templates), the
allowlist and its path validation, the invite URL that is shown once and never kept, and the list/get
fields that make a move visible. The cloud is the scripted FakeCloud: it records what it was sent."""
import json
import logging
import sqlite3

import pytest

from tests.cloud_support import (  # noqa: F401  (fixtures)
    SERVER_TOKEN, cloud, gacabit, http, link, mint, raw_db_bytes,
)
from tests.test_cloud_review_http import skill_server  # noqa: F401  (fixture)

GID = "a" * 32
TID = "b" * 32
INVITE_URL = "https://goodandcheapagent.com/join/SECRET_INVITE_TOKEN_9f8e7d6c5b4a"


@pytest.fixture
def linked(http, cloud):
    link(http, cloud)
    cloud.requests.clear()
    return http


def sent(cloud, method, path):
    """The one request the cloud got for this route: (json body or None, query dict)."""
    got = cloud.seen(method, path)
    assert len(got) == 1, [str(r.url) for r in cloud.requests]
    r = got[0]
    return (json.loads(r.content) if r.content else None), dict(r.url.params)


# route, cloud route, local request body, expected body at the cloud, expected query
FORWARDS = [
    ("GET", f"/gacabits/{GID}/members", None, None, {}),
    ("PATCH", f"/gacabits/{GID}/members/acct_x", {"role": "viewer"}, {"role": "viewer"}, {}),
    ("DELETE", f"/gacabits/{GID}/members/acct_x", None, None, {}),
    ("GET", f"/gacabits/{GID}/invites", None, None, {}),
    ("POST", f"/gacabits/{GID}/invites", {"role": "editor", "max_uses": 3}, {"role": "editor", "max_uses": 3, "user_confirmed": True}, {}),
    ("DELETE", f"/gacabits/{GID}/invites/inv_1", None, None, {}),
    ("GET", f"/gacabits/{GID}/validations", None, None, {}),
    ("POST", f"/gacabits/{GID}/validations", {"subject": "publish", "title": "T", "tags": ["a"]},
     {"subject": "publish", "title": "T", "tags": ["a"]}, {}),
    ("POST", f"/gacabits/{GID}/publish", {"confirmed_preview": True, "preview_hash": "h", "title": "T"},
     {"confirmed_preview": True, "preview_hash": "h", "title": "T", "user_confirmed": True}, {}),
    ("PATCH", f"/templates/{TID}", {"visibility": "listed"}, {"visibility": "listed", "user_confirmed": True}, {}),
    ("DELETE", f"/templates/{TID}", None, None, {"user_confirmed": "true"}),
    ("POST", f"/templates/{TID}/recheck", None, None, {}),
]


@pytest.mark.parametrize("method,cloud_path,body,expected_body,expected_query", FORWARDS,
                         ids=[f"{m} {p.replace(GID, 'G').replace(TID, 'T')}" for m, p, *_ in FORWARDS])
def test_each_route_goes_to_exactly_one_cloud_route(linked, cloud, method, cloud_path, body, expected_body, expected_query):
    cloud.on(method, cloud_path, status=200, json={"ok": True, "echo": 1}, headers={"etag": '"v1"'})
    r = linked.request(method, "/api/cloud" + cloud_path, headers=linked.session, json=body)
    assert r.status_code == 200 and r.json() == {"ok": True, "echo": 1}
    got_body, got_query = sent(cloud, method, cloud_path)
    assert got_body == expected_body and got_query == expected_query
    assert len(cloud.requests) == 1
    req = cloud.requests[0]
    assert req.headers["authorization"] == f"Bearer {SERVER_TOKEN}"  # the stored link, never the app's own token


@pytest.mark.parametrize("path,body", [
    (f"/gacabits/{GID}/invites", {"role": "viewer", "user_confirmed": False}),
    (f"/gacabits/{GID}/publish", {"confirmed_preview": True, "preview_hash": "h", "user_confirmed": False}),
    (f"/templates/{TID}", {"visibility": "listed", "user_confirmed": False}),
])
def test_user_confirmed_is_set_here_never_trusted_from_the_app(linked, cloud, path, body):
    method = "PATCH" if "templates" in path else "POST"
    cloud.on(method, path, status=200, json={})
    assert linked.request(method, "/api/cloud" + path, headers=linked.session, json=body).status_code == 200
    assert sent(cloud, method, path)[0]["user_confirmed"] is True


def test_calls_that_do_not_need_it_do_not_get_it(linked, cloud):
    cloud.on("POST", f"/gacabits/{GID}/validations", json={})
    cloud.on("PATCH", f"/gacabits/{GID}/members/acct_x", json={})
    linked.post(f"/api/cloud/gacabits/{GID}/validations", headers=linked.session, json={"subject": "share"})
    linked.patch(f"/api/cloud/gacabits/{GID}/members/acct_x", headers=linked.session, json={"role": "viewer"})
    assert "user_confirmed" not in json.dumps([json.loads(r.content) for r in cloud.requests])


def test_the_clouds_answers_come_back_unchanged(linked, cloud):
    for status, code in [(403, "forbidden_role"), (404, "not_found"), (409, "conflict"), (422, "validation_blocked"),
                         (429, "rate_limited"), (402, "billing_required"), (410, "gone")]:
        cloud.on("POST", f"/gacabits/{GID}/publish", status=status, json={"code": code, "message": "m", "detail": {"k": 1}},
                 headers={"retry-after": "7"})
        r = linked.post(f"/api/cloud/gacabits/{GID}/publish", headers=linked.session, json={})
        assert r.status_code == status and r.json() == {"code": code, "message": "m", "detail": {"k": 1}}
    assert r.headers["retry-after"] == "7"


def test_the_cloud_being_down_or_rejecting_the_link_is_a_502_never_a_401(linked, cloud):
    cloud.on("GET", f"/gacabits/{GID}/members", status=401, json={"code": "unauthenticated", "message": "no"})
    r = linked.get(f"/api/cloud/gacabits/{GID}/members", headers=linked.session)
    assert r.status_code == 502 and r.json()["code"] == "cloud_link_invalid"
    assert linked.get("/api/cloud/link", headers=linked.session).json()["state"] == "invalid"


def test_a_body_that_is_not_a_json_object_is_refused_before_the_cloud(linked, cloud):
    for raw in (b"[1]", b"not json", b'"s"'):
        r = linked.post(f"/api/cloud/gacabits/{GID}/invites", headers={**linked.session, "content-type": "application/json"},
                        content=raw)
        assert r.status_code == 422 and r.json()["code"] == "request_invalid"
    assert cloud.requests == []


def test_leaving_removes_the_pointer_only_when_the_cloud_accepted(linked, cloud):
    cloud.on("GET", "/gacabits", json={"gacabits": [gacabit(gid=GID)]})
    assert [a["id"] for a in linked.get("/api/artifacts", headers=linked.session).json()["artifacts"]] == [GID]
    cloud.on("POST", f"/gacabits/{GID}/leave", status=409, json={"code": "owner_cannot_leave", "message": "no"})
    r = linked.post(f"/api/cloud/gacabits/{GID}/leave", headers=linked.session)
    assert r.status_code == 409 and r.json()["code"] == "owner_cannot_leave"
    con = sqlite3.connect(linked.db)
    assert con.execute("SELECT count(*) FROM cloud_pointers").fetchone()[0] == 1
    cloud.on("POST", f"/gacabits/{GID}/leave", status=204)
    assert linked.post(f"/api/cloud/gacabits/{GID}/leave", headers=linked.session).status_code == 204
    assert con.execute("SELECT count(*) FROM cloud_pointers").fetchone()[0] == 0
    con.close()


# ── the invite link's secret ─────────────────────────────────────────────────
def test_the_invite_url_is_returned_once_and_never_stored_or_logged(linked, cloud, caplog):
    caplog.set_level(logging.DEBUG)
    created = {"id": "inv_1", "role": "viewer", "status": "active", "uses": 0, "max_uses": 1, "url": INVITE_URL}
    cloud.on("POST", f"/gacabits/{GID}/invites", status=201, json=created)
    r = linked.post(f"/api/cloud/gacabits/{GID}/invites", headers=linked.session, json={"role": "viewer"})
    assert r.status_code == 201 and r.json()["url"] == INVITE_URL  # exactly as the cloud sent it
    assert r.headers["cache-control"] == "no-store"
    # the list never has it (the cloud does not send it); this server did not keep a copy to add
    cloud.on("GET", f"/gacabits/{GID}/invites", json={"invite_links": [{k: v for k, v in created.items() if k != "url"}]})
    assert INVITE_URL not in linked.get(f"/api/cloud/gacabits/{GID}/invites", headers=linked.session).text
    # not in the logs at any level, not anywhere on disk
    assert "SECRET_INVITE_TOKEN" not in caplog.text
    assert b"SECRET_INVITE_TOKEN" not in raw_db_bytes(linked)
    linked.get("/api/artifacts", headers=linked.session)  # a list after the fact does not resurface it either
    assert "SECRET_INVITE_TOKEN" not in caplog.text


def test_redeeming_tells_the_cloud_the_person_tapped(linked, cloud):
    cloud.on("POST", "/invites/redeem", json=gacabit(gid=GID, role="editor"))
    assert linked.post("/api/cloud/invites/redeem", headers=linked.session, json={"token": "t"}).status_code == 200
    assert sent(cloud, "POST", "/invites/redeem")[0] == {"token": "t", "user_confirmed": True}


def test_template_ids_must_be_lowercase_hex_for_the_writing_routes(linked, cloud):
    for method, tail in (("PATCH", ""), ("DELETE", ""), ("POST", "/recheck")):
        r = linked.request(method, f"/api/cloud/templates/{'B' * 32}{tail}", headers=linked.session, json={})
        assert r.status_code == 422
    assert cloud.requests == []


# ── the allowlist: path ids ──────────────────────────────────────────────────
BAD_PATHS = [
    f"/api/cloud/gacabits/{'A' * 32}/members",  # uppercase hex
    f"/api/cloud/gacabits/{'a' * 31}/members",  # too short
    f"/api/cloud/gacabits/{'a' * 33}/members",  # too long
    f"/api/cloud/gacabits/{'g' * 32}/members",  # not hex
    "/api/cloud/gacabits/g1/members",
    "/api/cloud/gacabits/../members",
    f"/api/cloud/gacabits/{GID}/../members",
    f"/api/cloud/gacabits/{GID}%2f..%2fmembers/members",
    f"/api/cloud/gacabits/{GID}/members/a%2fb",
    f"/api/cloud/gacabits/{GID}/members/acct_x/extra",
    f"/api/cloud/gacabits/{GID}/members/a b",
    f"/api/cloud/gacabits/{GID}/members/%00",
    f"/api/cloud/gacabits/{GID}?x=1/members",
    f"/api/cloud/gacabits/{GID}/invites/inv%2F1",
    f"/api/cloud/gacabits/{GID}/invites/{'x' * 65}",
    f"/api/cloud/gacabits/{GID}/invites/a.b",
    f"/api/cloud/templates/t1/recheck",
    f"/api/cloud/templates/{TID}/recheck/extra",
]


@pytest.mark.parametrize("path", BAD_PATHS)
def test_ids_and_paths_outside_the_allowlist_never_reach_the_cloud(linked, cloud, path):
    for method in ("GET", "POST", "PATCH", "DELETE"):
        r = linked.request(method, path, headers=linked.session, json={"role": "viewer"} if method in ("POST", "PATCH") else None)
        assert r.status_code in (404, 405, 422), (method, path, r.status_code, r.text)
    assert cloud.requests == []


@pytest.mark.parametrize("method,path", [
    ("DELETE", f"/api/cloud/gacabits/{GID}"), ("PATCH", f"/api/cloud/gacabits/{GID}"), ("GET", f"/api/cloud/gacabits/{GID}"),
    ("POST", f"/api/cloud/gacabits/{GID}/export"), ("POST", f"/api/cloud/gacabits/{GID}/bring-home"),
    ("POST", f"/api/cloud/gacabits/{GID}/restore"), ("PUT", f"/api/cloud/gacabits/{GID}/state"),
    ("POST", f"/api/cloud/gacabits/{GID}/members"), ("GET", "/api/cloud/me"), ("DELETE", "/api/cloud/me"),
    ("GET", "/api/cloud/me/servers"), ("POST", "/api/cloud/uploads"), ("POST", "/api/cloud/gacabits/import"),
    ("GET", "/api/cloud/billing"), ("POST", "/api/cloud/admin/templates/recheck"), ("GET", "/api/cloud/validations/v1"),
    ("POST", "/api/cloud/validations/v1/acknowledge"), ("PUT", f"/api/cloud/gacabits/{GID}/members/acct_x"),
    ("PUT", f"/api/cloud/templates/{TID}"), ("POST", f"/api/cloud/templates/{TID}/report"),
    ("GET", "/api/cloud/auth/google"), ("GET", "//api/cloud/gacabits"),
])
def test_routes_the_cloud_has_but_the_app_may_not_use_do_not_exist_here(linked, cloud, method, path):
    r = linked.request(method, path, headers=linked.session, json={})
    assert r.status_code in (404, 405), (r.status_code, r.text)
    assert cloud.requests == []


# ── paired device only (the rest of the table is in test_cloud_move_http.py) ──
def test_an_api_token_with_every_scope_cannot_share_or_publish(linked, cloud):
    token = mint(linked, scopes=("alerts", "briefings", "artifacts", "heartbeat", "cloud"))
    for method, path, body in [
        ("POST", f"/api/cloud/gacabits/{GID}/invites", {"role": "viewer"}),
        ("POST", f"/api/cloud/gacabits/{GID}/publish", {}),
        ("PATCH", f"/api/cloud/templates/{TID}", {"visibility": "listed"}),
        ("DELETE", f"/api/cloud/templates/{TID}", None),
    ]:
        assert linked.request(method, path, headers=token, json=body).status_code == 403
    assert cloud.requests == []


# ── the list / get fields ────────────────────────────────────────────────────
def test_cloud_pages_carry_the_clouds_status_and_never_moving(linked, cloud):
    cloud.on("GET", "/gacabits", json={"gacabits": [
        gacabit(gid=GID, role="owner", status="brought_home"), gacabit(gid="c" * 32, status="active")]})
    items = {a["id"]: a for a in linked.get("/api/artifacts", headers=linked.session).json()["artifacts"]}
    assert items[GID]["cloud_status"] == "brought_home" and items[GID]["moving"] is False and items[GID]["home"] == "cloud"
    assert items["c" * 32]["cloud_status"] == "active"
    cloud.on("GET", f"/gacabits/{GID}", json=gacabit(gid=GID, role="owner", status="brought_home"))
    got = linked.get(f"/api/artifacts/{GID}", headers=linked.session).json()
    assert got["home"] == "cloud" and got["cloud_status"] == "brought_home" and got["moving"] is False


def test_local_pages_say_where_they_live(linked, cloud):
    a = linked.post("/api/artifacts", headers=linked.session, json={"title": "Mine", "html": "<p>x</p>"}).json()
    got = linked.get(f"/api/artifacts/{a['id']}", headers=linked.session).json()
    assert got["home"] == "local" and got["moving"] is False and got["cloud_status"] is None
    assert linked.get("/api/artifacts", headers=linked.session).json()["artifacts"][0]["home"] == "local"


# ── the Hermes skill ─────────────────────────────────────────────────────────
def test_skill_list_shows_a_page_that_is_moving_and_a_cloud_status(skill_server):
    answers, run = skill_server
    moving = {"id": "m1", "title": "Moving", "version": 1, "updated_at": "2026-11-02T10:00:00Z", "home": "local",
              "role": "owner", "moving": True, "cloud_status": "moving"}
    locked = {"id": "g1", "title": "Locked", "version": 1, "updated_at": "2026-11-02T10:00:00Z", "home": "cloud",
              "role": "owner", "cloud_status": "brought_home"}
    answers.update(status=200, body={"artifacts": [moving, locked], "stale": False})
    out = run("list").stdout
    assert "being moved to the cloud" in out and "cloud status: brought_home" in out


def test_the_skill_has_no_command_to_move_share_or_publish(skill_server):
    import re

    _, run = skill_server
    helptext = run("--help").stdout + run("--help").stderr
    commands = re.findall(r"\{([a-z0-9,_-]+)\}", helptext)
    names = set(",".join(commands).split(","))
    # (`publish` IS a command: it creates a local page. It has nothing to do with publishing to the gallery.)
    for forbidden in ("move", "move-to-cloud", "bring-home", "undo-bring-home", "share", "invite", "members", "leave",
                      "unlink", "link", "cloud", "gallery"):
        assert forbidden not in names, names
    assert names  # we did read the command list
