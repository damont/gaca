"""HTTP-level checks for /api/lists: routing, validation, and per-user isolation."""
import pytest
from fastapi.testclient import TestClient

from gaca_api.config import settings
from gaca_api.main import app
from gaca_api.models.user import UserDoc
from gaca_api.utils.auth import get_current_user

_current = {"id": "u1"}


@pytest.fixture
def http(tmp_path):
    settings.db_path = str(tmp_path / "t.db")
    app.dependency_overrides[get_current_user] = lambda: UserDoc(
        id=_current["id"], email=f"{_current['id']}@example.com", display_name="T"
    )
    _current["id"] = "u1"
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_a_shopping_trip_end_to_end(http):
    made = http.post("/api/lists", json={"name": "Costco", "kind": "shopping"})
    assert made.status_code == 201
    lid = made.json()["id"]
    assert http.post("/api/lists", json={"name": "costco"}).status_code == 422  # same name, loosely

    match = http.get("/api/lists/match", params={"q": "the Costco run"}).json()
    assert match["match"]["id"] == lid

    add = http.post(f"/api/lists/{lid}/items", json={"items": [
        {"text": "batteries", "category": "household"}, {"text": "2% milk", "category": "dairy", "quantity": "2"},
        {"text": "paper towels"}]})
    assert add.status_code == 201
    body = add.json()
    assert [i["text"] for i in body["added"]] == ["batteries", "2% milk", "paper towels"]
    assert body["list"]["open_count"] == 3

    dup = http.post(f"/api/lists/{lid}/items", json={"items": [{"text": "Battery"}, {"text": "milk"}]}).json()
    assert [d["text"] for d in dup["duplicates"]] == ["Battery"]
    assert dup["added"][0]["similar_to"] == "2% milk"

    checked = http.post(f"/api/lists/{lid}/items/check", json={"texts": ["batteries", "paper towel", "bagels"]}).json()
    assert sorted(i["text"] for i in checked["matched"]) == ["batteries", "paper towels"]
    assert checked["not_found"] == ["bagels"]

    detail = http.get(f"/api/lists/{lid}").json()
    assert [(i["text"], i["done"]) for i in detail["items"]][-2:] == [("paper towels", True), ("batteries", True)] \
        or {i["text"] for i in detail["items"] if i["done"]} == {"batteries", "paper towels"}
    assert detail["open_count"] == 2 and detail["done_count"] == 2

    assert http.post(f"/api/lists/{lid}/clear-done").json() == {"removed": 2}
    summaries = http.get("/api/lists").json()["lists"]
    assert [(s["name"], s["open_count"], s["done_count"]) for s in summaries] == [("Costco", 2, 0)]


def test_validation(http):
    lid = http.post("/api/lists", json={"name": "L"}).json()["id"]
    assert http.post("/api/lists", json={"name": "   "}).status_code == 422
    assert http.post("/api/lists", json={"name": "x", "kind": "bogus"}).status_code == 422
    assert http.post(f"/api/lists/{lid}/items", json={"items": []}).status_code == 422
    assert http.post(f"/api/lists/{lid}/items", json={"items": [{"text": "x", "url": "javascript:alert(1)"}]}).status_code == 422
    assert http.post(f"/api/lists/{lid}/items", json={"items": [{"text": "gift", "url": "https://shop.example/x"}]}).status_code == 201
    assert http.get("/api/lists/match").status_code == 422
    assert http.get("/api/lists/nope").status_code == 404


def test_other_users_cannot_see_or_touch_your_lists(http):
    lid = http.post("/api/lists", json={"name": "Private"}).json()["id"]
    item = http.post(f"/api/lists/{lid}/items", json={"items": [{"text": "secret"}]}).json()["added"][0]
    _current["id"] = "u2"
    assert http.get("/api/lists").json() == {"lists": []}
    assert http.get(f"/api/lists/{lid}").status_code == 404
    assert http.post(f"/api/lists/{lid}/items", json={"items": [{"text": "x"}]}).status_code == 404
    assert http.patch(f"/api/lists/{lid}/items/{item['id']}", json={"done": True}).status_code == 404
    assert http.delete(f"/api/lists/{lid}").status_code == 404
    assert http.get("/api/lists/match", params={"q": "private"}).json()["match"] is None
    _current["id"] = "u1"
    assert http.get(f"/api/lists/{lid}").json()["items"][0]["done"] is False
