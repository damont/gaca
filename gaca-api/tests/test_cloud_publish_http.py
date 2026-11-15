"""Part 8e: publish a page to the gallery, for the app. One pair of routes (`publish-check`, `publish`) that
works for a private page (this server sends the page to the cloud's /templates/preview and /templates/publish)
and for a cloud page (forwarded to the cloud's gacabit routes). Never sends data."""
import json

import pytest

from gaca_api.config import settings
from gaca_api.services import artifact_locks
from tests.cloud_support import (  # noqa: F401  (fixtures)
    cloud, gacabit, http, link, mint, script_link,
)

HASH = "a" * 64
TID = "tmpl_" + "1" * 20
LISTING = {"title": "Costco", "description": "A list", "tags": ["groceries"], "category": "lists", "license": "cc-by-4.0"}


def make_private(http, **extra):
    body = {
        "title": "Costco run", "html": "<h1>page</h1>", "description": "PRIVATE-DESC", "data_notes": "items: {text}",
        "state": {"secret": "STATE-SECRET"},
        "schemas": {"items": {"fields": {"text": {"type": "string", "required": True}}}},
        "collections": {"items": {"i1": {"text": "DATA-MILK"}}}, **extra,
    }
    r = http.post("/api/artifacts", headers=http.session, json=body)
    assert r.status_code == 201, r.text
    aid = r.json()["id"]
    http.post(f"/api/artifacts/{aid}/events", headers=http.session, json={"name": "EVENT-NAME", "payload": {"x": "EVENT-PAYLOAD"}})
    http.patch(f"/api/artifacts/{aid}", headers=http.session, json={"html": "<h1>page v2</h1>", "note": "VERSION-NOTE"})
    return aid


def script_preview(cloud, status="passed"):
    cloud.on("POST", "/templates/preview", json={"status": status, "findings": [], "ai_review_missing": True,
                                                  "content_hash": HASH, "preview": {"html": "<h1>page v2</h1>", "schemas": {}}})


def script_publish(cloud):
    cloud.on("POST", "/templates/publish", status=201, json={"id": TID, "title": "Costco", "version": 1, "status": "checking"})


def sent(cloud, method, path):
    reqs = cloud.seen(method, path)
    assert len(reqs) == 1, [(r.method, r.url.path) for r in cloud.requests]
    return json.loads(reqs[0].content)


def row(http, aid):
    return next(a for a in http.get("/api/artifacts", headers=http.session).json()["artifacts"] if a["id"] == aid)


# ── a private page ───────────────────────────────────────────────────────────
def test_private_check_sends_the_page_and_never_the_data(http, cloud):
    link(http, cloud)
    script_preview(cloud)
    aid = make_private(http)
    r = http.post(f"/api/artifacts/{aid}/publish-check", headers=http.session, json=LISTING)
    assert r.status_code == 200 and r.json()["content_hash"] == HASH and r.json()["status"] == "passed"
    body = sent(cloud, "POST", "/templates/preview")
    assert body["source"]["id"] == aid and body["source"]["html"] == "<h1>page v2</h1>"  # the CURRENT page
    assert body["source"]["schemas"]["items"]["fields"]["text"]["type"] == "string"
    assert set(body["source"]) == {"id", "html", "schemas"}  # no data_notes unless asked, no state, no collections
    assert "user_confirmed" not in body and body["title"] == "Costco"
    wire = json.dumps(body)
    for secret in ("DATA-MILK", "STATE-SECRET", "EVENT-NAME", "EVENT-PAYLOAD", "VERSION-NOTE", "PRIVATE-DESC", "<h1>page</h1>"):
        assert secret not in wire, secret
    assert cloud.seen("POST", "/templates/publish") == [] and cloud.seen("POST", f"/gacabits/{aid}/validations") == []


def test_data_notes_only_go_when_the_person_asks(http, cloud):
    link(http, cloud)
    script_preview(cloud)
    aid = make_private(http)
    http.post(f"/api/artifacts/{aid}/publish-check", headers=http.session, json={**LISTING, "include_data_notes": True})
    body = sent(cloud, "POST", "/templates/preview")
    assert body["source"]["data_notes"] == "items: {text}" and body["include_data_notes"] is True


def test_the_title_falls_back_to_the_pages_title(http, cloud):
    link(http, cloud)
    script_preview(cloud)
    aid = make_private(http)
    http.post(f"/api/artifacts/{aid}/publish-check", headers=http.session, json={})
    assert sent(cloud, "POST", "/templates/preview")["title"] == "Costco run"


def test_private_publish_confirms_for_the_person_remembers_the_template_and_lists_it(http, cloud):
    link(http, cloud)
    script_publish(cloud)
    aid = make_private(http)
    before = row(http, aid)
    assert before["published_template_id"] is None
    r = http.post(f"/api/artifacts/{aid}/publish", headers=http.session,
                  json={**LISTING, "visibility": "listed", "confirmed_preview": True, "preview_hash": HASH})
    assert r.status_code == 201 and r.json()["id"] == TID and r.json()["status"] == "checking"
    body = sent(cloud, "POST", "/templates/publish")
    assert body["user_confirmed"] is True and body["confirmed_preview"] is True and body["preview_hash"] == HASH
    assert body["source"]["id"] == aid and "DATA-MILK" not in json.dumps(body)
    assert row(http, aid)["published_template_id"] == TID and row(http, aid)["home"] == "local"
    assert http.get(f"/api/artifacts/{aid}", headers=http.session).json()["published_template_id"] == TID
    # remembering it is not an edit: the page's revision and time are untouched
    after = row(http, aid)
    assert (after["rev"], after["version"], after["updated_at"]) == (before["rev"], before["version"], before["updated_at"])


def test_the_app_cannot_set_user_confirmed_or_smuggle_a_source(http, cloud):
    link(http, cloud)
    aid = make_private(http)
    for extra in ({"user_confirmed": True}, {"source": {"id": "x", "html": "<b>"}}, {"state": {"a": 1}}, {"collections": {}}):
        r = http.post(f"/api/artifacts/{aid}/publish", headers=http.session,
                      json={**LISTING, "confirmed_preview": True, "preview_hash": HASH, **extra})
        assert r.status_code == 422, extra
    assert cloud.seen("POST", "/templates/publish") == []


def test_a_hash_mismatch_is_passed_through_and_nothing_is_remembered(http, cloud):
    link(http, cloud)
    cloud.on("POST", "/templates/publish", status=409,
             json={"code": "preview_mismatch", "message": "The page changed since the preview.", "detail": {"preview_hash": HASH}})
    aid = make_private(http)
    r = http.post(f"/api/artifacts/{aid}/publish", headers=http.session,
                  json={**LISTING, "confirmed_preview": True, "preview_hash": "0" * 64})
    assert r.status_code == 409 and r.json()["code"] == "preview_mismatch" and r.json()["detail"]
    assert row(http, aid)["published_template_id"] is None


def test_publishing_again_keeps_the_same_template_id(http, cloud):
    link(http, cloud)
    script_publish(cloud)
    aid = make_private(http)
    for _ in range(2):
        assert http.post(f"/api/artifacts/{aid}/publish", headers=http.session,
                         json={**LISTING, "confirmed_preview": True, "preview_hash": HASH}).status_code == 201
    assert row(http, aid)["published_template_id"] == TID
    assert sent_both(cloud)[0]["source"]["id"] == sent_both(cloud)[1]["source"]["id"] == aid


def sent_both(cloud):
    return [json.loads(r.content) for r in cloud.seen("POST", "/templates/publish")]


# ── a page already in the cloud ──────────────────────────────────────────────
@pytest.fixture
def cloud_page(http, cloud):
    link(http, cloud)
    gid = "c" * 32
    cloud.on("GET", "/gacabits", json={"gacabits": [gacabit(gid, title="Shared", role="owner", owner="david")], "next_cursor": None})
    http.get("/api/artifacts", headers=http.session)
    cloud.requests.clear()
    return gid


def test_a_cloud_page_forwards_to_the_gacabit_routes(http, cloud, cloud_page):
    gid = cloud_page
    cloud.on("POST", f"/gacabits/{gid}/validations", json={"status": "passed", "findings": [], "content_hash": HASH, "preview": {"html": "x"}})
    cloud.on("POST", f"/gacabits/{gid}/publish", status=201, json={"id": TID, "status": "checking", "version": 1})
    r = http.post(f"/api/artifacts/{gid}/publish-check", headers=http.session, json=LISTING)
    assert r.status_code == 200 and r.json()["content_hash"] == HASH
    check = sent(cloud, "POST", f"/gacabits/{gid}/validations")
    assert check["subject"] == "publish" and check["title"] == "Costco" and "source" not in check
    r = http.post(f"/api/artifacts/{gid}/publish", headers=http.session,
                  json={**LISTING, "visibility": "unlisted", "confirmed_preview": True, "preview_hash": HASH})
    assert r.status_code == 201 and r.json()["id"] == TID
    body = sent(cloud, "POST", f"/gacabits/{gid}/publish")
    assert body["user_confirmed"] is True and body["visibility"] == "unlisted" and "source" not in body
    assert cloud.seen("POST", "/templates/preview") == [] and cloud.seen("POST", "/templates/publish") == []


def test_a_cloud_pages_published_template_id_comes_from_the_cloud(http, cloud, cloud_page):
    gid = cloud_page
    cloud.on("GET", "/gacabits", json={"gacabits": [gacabit(gid, title="Shared", role="owner", owner="david", published_template_id=TID)],
                                       "next_cursor": None})
    cloud.on("POST", f"/gacabits/{gid}/publish", status=201, json={"id": TID, "status": "checking", "version": 1})
    http.post(f"/api/artifacts/{gid}/publish", headers=http.session, json={**LISTING, "confirmed_preview": True, "preview_hash": HASH})
    assert row(http, gid)["published_template_id"] == TID and row(http, gid)["home"] == "cloud"


def test_a_cloud_error_passes_through_for_a_cloud_page(http, cloud, cloud_page):
    gid = cloud_page
    cloud.on("POST", f"/gacabits/{gid}/publish", status=409, json={"code": "preview_mismatch", "message": "changed"})
    r = http.post(f"/api/artifacts/{gid}/publish", headers=http.session, json={**LISTING, "confirmed_preview": True, "preview_hash": "0" * 64})
    assert r.status_code == 409 and r.json()["code"] == "preview_mismatch"


# ── refusals, each with its own code, before anything is sent ────────────────
def test_not_linked(http, cloud):
    aid = make_private(http)
    r = http.post(f"/api/artifacts/{aid}/publish-check", headers=http.session, json=LISTING)
    assert r.status_code == 409 and r.json()["code"] == "cloud_not_linked" and r.json()["message"]
    assert cloud.requests == []


def test_cloud_not_configured(http, cloud, monkeypatch):
    aid = make_private(http)
    monkeypatch.setattr(settings, "cloud_url", "")
    r = http.post(f"/api/artifacts/{aid}/publish", headers=http.session, json={**LISTING, "confirmed_preview": True, "preview_hash": HASH})
    assert r.status_code == 409 and r.json()["code"] == "cloud_not_configured"
    assert cloud.requests == []


def test_a_read_only_link_cannot_publish(http, cloud):
    link(http, cloud, scope="read")
    aid = make_private(http)
    cloud.requests.clear()
    for path, body in (("publish-check", LISTING), ("publish", {**LISTING, "confirmed_preview": True, "preview_hash": HASH})):
        r = http.post(f"/api/artifacts/{aid}/{path}", headers=http.session, json=body)
        assert r.status_code == 403 and r.json()["code"] == "cloud_link_read_only", path
    assert cloud.requests == []


def test_archived_and_moving_pages_are_refused(http, cloud):
    link(http, cloud)
    cloud.requests.clear()
    aid = make_private(http)
    http.post(f"/api/artifacts/{aid}/archive", headers=http.session)
    http.patch(f"/api/artifacts/{aid}", headers=http.session, json={"archived": True})
    r = http.post(f"/api/artifacts/{aid}/publish-check", headers=http.session, json=LISTING)
    assert r.status_code == 409 and r.json()["code"] == "artifact_archived"
    http.patch(f"/api/artifacts/{aid}", headers=http.session, json={"archived": False})
    artifact_locks.MOVING.add(aid)
    try:
        r = http.post(f"/api/artifacts/{aid}/publish", headers=http.session, json={**LISTING, "confirmed_preview": True, "preview_hash": HASH})
        assert r.status_code == 423 and r.json()["code"] == "artifact_moving"
    finally:
        artifact_locks.MOVING.discard(aid)
    assert cloud.requests == []


def test_unknown_page_and_agent_tokens(http, cloud):
    link(http, cloud)
    cloud.requests.clear()
    assert http.post("/api/artifacts/nope/publish-check", headers=http.session, json=LISTING).status_code == 404
    aid = make_private(http)
    hermes = mint(http, name="hermes-x", scopes=("artifacts", "cloud"))
    for path, body in (("publish-check", LISTING), ("publish", {**LISTING, "confirmed_preview": True, "preview_hash": HASH})):
        assert http.post(f"/api/artifacts/{aid}/{path}", headers=hermes, json=body).status_code == 403
        assert http.post(f"/api/artifacts/{aid}/{path}", json=body).status_code == 401
    assert cloud.requests == []


def test_the_old_forwarded_publish_route_still_works(http, cloud, cloud_page):
    gid = cloud_page
    cloud.on("POST", f"/gacabits/{gid}/publish", status=201, json={"id": TID, "status": "checking", "version": 1})
    r = http.post(f"/api/cloud/gacabits/{gid}/publish", headers=http.session, json={**LISTING, "confirmed_preview": True, "preview_hash": HASH})
    assert r.status_code == 201 and sent(cloud, "POST", f"/gacabits/{gid}/publish")["user_confirmed"] is True
