"""HTTP-level checks for /api/artifacts: pages, versions, state, collections, events, scoping."""
import pytest
from fastapi.testclient import TestClient

from gaca_api.config import settings
from gaca_api.main import app
from gaca_api.models.user import UserDoc
from gaca_api.services.artifacts import merge_patch
from gaca_api.utils.auth import get_current_user

_current = {"id": "u1"}


@pytest.fixture
def http(tmp_path):
    settings.db_path = str(tmp_path / "t.db")
    app.dependency_overrides[get_current_user] = lambda: UserDoc(
        id=_current["id"], email=f"{_current['id']}@example.com", display_name="Test"
    )
    _current["id"] = "u1"
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def make(http, **kw):
    body = {"title": "Costco", "html": "<h1>Costco</h1>", **kw}
    r = http.post("/api/artifacts", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def test_merge_patch_rfc7386():
    assert merge_patch({"a": 1, "b": {"c": 2, "d": 3}}, {"b": {"c": None, "e": 4}, "f": [1]}) == {
        "a": 1, "b": {"d": 3, "e": 4}, "f": [1]}
    assert merge_patch({"a": [1, 2]}, {"a": [3]}) == {"a": [3]}


def test_create_get_html_and_link(http):
    a = make(http, description="Weekly run", state={"x": 1})
    assert a["link"] == f"gaca://artifact/{a['id']}" and a["version"] == 1 and a["state_version"] == 1
    r = http.get(f"/api/artifacts/{a['id']}/html")
    assert r.text == "<h1>Costco</h1>" and r.headers["content-type"].startswith("text/html")
    assert "sandbox" in r.headers["content-security-policy"]
    assert [x["id"] for x in http.get("/api/artifacts").json()["artifacts"]] == [a["id"]]
    assert http.get("/api/artifacts", params={"q": "weekly"}).json()["artifacts"][0]["id"] == a["id"]
    assert http.get("/api/artifacts", params={"q": "zzz"}).json()["artifacts"] == []


def test_validation(http):
    assert http.post("/api/artifacts", json={"title": " ", "html": "x"}).status_code == 422
    assert http.post("/api/artifacts", json={"title": "t", "html": ""}).status_code == 422
    assert http.post("/api/artifacts", json={"title": "t", "html": "x" * 1_100_000}).status_code == 422
    assert http.post("/api/artifacts", json={"title": "t", "html": "x", "collections": {"Bad Name": {}}}).status_code == 422
    assert http.post("/api/artifacts", json={"title": "t", "html": "x", "session_id": "nope"}).status_code == 404
    assert http.post("/api/artifacts", json={"title": "t", "html": "x", "alert_id": "nope"}).status_code == 404


def test_rewrite_makes_versions_and_keeps_data(http):
    a = make(http, collections={"items": {"milk": {"text": "Milk"}}})
    r = http.patch(f"/api/artifacts/{a['id']}", json={"html": "<h1>v2</h1>", "note": "bigger"})
    assert r.json()["version"] == 2
    assert http.get(f"/api/artifacts/{a['id']}/html").text == "<h1>v2</h1>"
    assert http.get(f"/api/artifacts/{a['id']}/html", params={"version": 1}).text == "<h1>Costco</h1>"
    assert http.get(f"/api/artifacts/{a['id']}/html", params={"version": 9}).status_code == 404
    v = http.get(f"/api/artifacts/{a['id']}/versions").json()
    assert v["current"] == 2 and [x["note"] for x in v["versions"]] == ["bigger", "created"]
    assert http.get(f"/api/artifacts/{a['id']}/collections/items").json()["docs"][0]["data"] == {"text": "Milk"}
    r = http.post(f"/api/artifacts/{a['id']}/restore", json={"version": 1})
    assert r.json()["version"] == 3
    assert http.get(f"/api/artifacts/{a['id']}/html").text == "<h1>Costco</h1>"


def test_metadata_patch_archive_and_clear_description(http):
    a = make(http, description="d")
    r = http.patch(f"/api/artifacts/{a['id']}", json={"title": "  New   name ", "description": None, "archived": True})
    assert r.json()["title"] == "New name" and r.json()["description"] is None and r.json()["version"] == 1
    assert http.get("/api/artifacts").json()["artifacts"] == []
    assert len(http.get("/api/artifacts", params={"include_archived": True}).json()["artifacts"]) == 1


def test_state_put_patch_and_conflicts(http):
    a = make(http)
    base = f"/api/artifacts/{a['id']}/state"
    assert http.get(base).json() == {"state": {}, "version": 0, "rev": 0}
    r = http.put(base, json={"state": {"a": 1, "n": {"x": 1}}, "expected_version": 0}).json()
    assert r["version"] == 1
    r = http.patch(base, json={"patch": {"n": {"y": 2}, "a": None}, "expected_version": 1}).json()
    assert r["state"] == {"n": {"x": 1, "y": 2}} and r["version"] == 2
    assert http.put(base, json={"state": {}, "expected_version": 1}).status_code == 409
    assert http.patch(base, json={"patch": {"z": 1}, "expected_version": 1}).status_code == 409
    assert http.get(base).json()["state"] == {"n": {"x": 1, "y": 2}}
    assert http.put(base, json={"state": {"big": "x" * 1_100_000}}).status_code == 413


def test_collections_crud_and_ordering(http):
    a = make(http)
    c = f"/api/artifacts/{a['id']}/collections/items"
    assert http.get(c).json()["docs"] == []
    d1 = http.post(c, json={"data": {"text": "milk"}}).json()
    d2 = http.post(c, json={"data": {"text": "eggs"}, "id": "eggs"}).json()
    assert d2["id"] == "eggs" and d1["version"] == 1
    assert http.post(c, json={"data": {}, "id": "eggs"}).status_code == 409
    assert [d["data"]["text"] for d in http.get(c).json()["docs"]] == ["milk", "eggs"]
    r = http.patch(f"{c}/eggs", json={"patch": {"done": True}, "expected_version": 1}).json()
    assert r["data"] == {"text": "eggs", "done": True} and r["version"] == 2
    assert http.patch(f"{c}/eggs", json={"patch": {"done": False}, "expected_version": 1}).status_code == 409
    assert http.put(f"{c}/eggs", json={"data": {"text": "x"}, "expected_version": 5}).status_code == 409
    assert http.put(f"{c}/new", json={"data": {"text": "new"}}).json()["version"] == 1
    assert http.put(f"{c}/new", json={"data": {"text": "new"}, "expected_version": 3}).status_code == 409
    assert http.patch(f"{c}/ghost", json={"patch": {}}).status_code == 404
    assert http.delete(f"{c}/eggs").status_code == 204 and http.delete(f"{c}/eggs").status_code == 404
    assert http.get(f"{c}/new").json()["data"] == {"text": "new"}
    assert http.get(f"{c}/Bad!").status_code == 422
    assert http.delete(c).status_code == 204
    assert http.get(c).json()["docs"] == []


def test_snapshot_and_rev_polling(http):
    a = make(http)
    base = f"/api/artifacts/{a['id']}"
    r0 = http.get(f"{base}/data").json()["rev"]
    http.post(f"{base}/collections/items", json={"data": {"t": 1}})
    snap = http.get(f"{base}/data", params={"since_rev": r0}).json()
    assert snap["changed"] and snap["rev"] > r0 and len(snap["collections"]["items"]) == 1
    same = http.get(f"{base}/data", params={"since_rev": snap["rev"]}).json()
    assert same == {"rev": snap["rev"], "changed": False, "state": None, "state_version": None, "collections": None}
    http.patch(f"{base}/state", json={"patch": {"k": 1}})
    assert http.get(f"{base}/data", params={"since_rev": snap["rev"]}).json()["state"] == {"k": 1}


def test_batch_is_all_or_nothing(http):
    a = make(http, collections={"items": {"a": {"n": 1}}})
    base = f"/api/artifacts/{a['id']}"
    ok = http.post(f"{base}/batch", json={"ops": [
        {"op": "set", "collection": "items", "id": "b", "data": {"n": 2}},
        {"op": "patch", "collection": "items", "id": "a", "data": {"n": 10}},
        {"op": "delete", "collection": "items", "id": "b"}]})
    assert ok.json()["applied"] == 3
    assert http.get(f"{base}/collections/items").json()["docs"][0]["data"] == {"n": 10}
    bad = http.post(f"{base}/batch", json={"ops": [
        {"op": "set", "collection": "items", "id": "c", "data": {"n": 3}},
        {"op": "delete", "collection": "items", "id": "nope"}]})
    assert bad.status_code == 404
    ids = [d["id"] for d in http.get(f"{base}/collections/items").json()["docs"]]
    assert ids == ["a"]
    assert http.post(f"{base}/batch", json={"ops": [{"op": "set", "collection": "items", "data": {}}]}).status_code == 422
    assert http.post(f"{base}/batch", json={"ops": [{"op": "clear", "collection": "items"}]}).status_code == 200
    assert http.get(f"{base}/collections/items").json()["docs"] == []


def test_events_outbox(http):
    a = make(http)
    base = f"/api/artifacts/{a['id']}/events"
    e1 = http.post(base, json={"name": "checked-out", "payload": {"n": 3}}).json()
    e2 = http.post(base, json={"name": "picked", "payload": {}}).json()
    assert [e["name"] for e in http.get(base).json()["events"]] == ["checked-out", "picked"]
    assert [e["seq"] for e in http.get(base, params={"after": e1["seq"]}).json()["events"]] == [e2["seq"]]
    assert http.post(f"{base}/handled", json={"upto": e1["seq"]}).json() == {"handled": 1}
    assert [e["name"] for e in http.get(base, params={"unhandled": True}).json()["events"]] == ["picked"]


def test_alert_can_carry_an_artifact(http):
    a = make(http)
    r = http.post("/api/alerts", json={"kind": "x.y", "title": "Look", "artifact_id": a["id"]})
    assert r.status_code == 201 and r.json()["artifact_id"] == a["id"]
    assert http.get(f"/api/alerts/{r.json()['id']}").json()["artifact_id"] == a["id"]
    assert http.post("/api/alerts", json={"kind": "x.y", "title": "Look", "artifact_id": "nope"}).status_code == 404
    # created for an alert -> filterable by it
    b = make(http, alert_id=r.json()["id"])
    assert [x["id"] for x in http.get("/api/artifacts", params={"alert_id": r.json()["id"]}).json()["artifacts"]] == [b["id"]]


def test_opened_does_not_change_updated_at_and_delete_removes_everything(http):
    a = make(http, collections={"items": {"a": {}}})
    base = f"/api/artifacts/{a['id']}"
    assert http.post(f"{base}/opened").status_code == 204
    after = http.get(base).json()
    assert after["last_opened_at"] and after["updated_at"] == a["updated_at"]
    assert http.delete(base).status_code == 204
    for path in ("", "/html", "/data", "/state", "/events", "/versions"):
        assert http.get(base + path).status_code == 404


def test_users_are_isolated(http):
    a = make(http)
    base = f"/api/artifacts/{a['id']}"
    _current["id"] = "u2"
    assert http.get("/api/artifacts").json()["artifacts"] == []
    for path in ("", "/html", "/data", "/state", "/events", "/versions"):
        assert http.get(base + path).status_code == 404
    assert http.patch(base, json={"title": "x"}).status_code == 404
    assert http.put(f"{base}/state", json={"state": {}}).status_code == 404
    assert http.post(f"{base}/collections/items", json={"data": {}}).status_code == 404
    assert http.delete(base).status_code == 404
    assert http.post("/api/alerts", json={"kind": "x.y", "title": "t", "artifact_id": a["id"]}).status_code == 404
    _current["id"] = "u1"
    assert http.get(base).status_code == 200


def test_reorder_artifacts(http):
    a, b, c = (make(http, title=t)["id"] for t in ("A", "B", "C"))
    # Never placed: newest first.
    assert [x["id"] for x in http.get("/api/artifacts").json()["artifacts"]] == [c, b, a]
    r = http.put("/api/artifacts/order", json={"ids": [a, c, b]})
    assert r.status_code == 200
    assert [x["id"] for x in r.json()["artifacts"]] == [a, c, b]
    assert [x["position"] for x in r.json()["artifacts"]] == [0, 1, 2]
    # Editing a page doesn't move it; a brand-new page appears on top until placed.
    http.patch(f"/api/artifacts/{b}", json={"description": "touched"})
    d = make(http, title="D")["id"]
    assert [x["id"] for x in http.get("/api/artifacts").json()["artifacts"]] == [d, a, c, b]
    # A partial list puts those first and keeps the rest in order.
    r = http.put("/api/artifacts/order", json={"ids": [b]})
    assert [x["id"] for x in r.json()["artifacts"]] == [b, d, a, c]


def test_reorder_rejects_unknown_and_other_users_ids(http):
    a = make(http)["id"]
    assert http.put("/api/artifacts/order", json={"ids": [a, "nope"]}).status_code == 404
    _current["id"] = "u2"
    assert http.put("/api/artifacts/order", json={"ids": [a]}).status_code == 404
    assert http.get("/api/artifacts").json()["artifacts"] == []


def test_reorder_keeps_archived_pages_out_of_the_list_but_in_the_order(http):
    a, b = make(http, title="A")["id"], make(http, title="B")["id"]
    http.patch(f"/api/artifacts/{a}", json={"archived": True})
    r = http.put("/api/artifacts/order", json={"ids": [b]})
    assert [x["id"] for x in r.json()["artifacts"]] == [b]
    assert http.get(f"/api/artifacts/{a}").json()["position"] == 1
