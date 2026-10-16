"""Per-collection schemas: every write is checked, and the error says what is wrong."""
import pytest
from fastapi.testclient import TestClient

from gaca_api.config import settings
from gaca_api.main import app
from gaca_api.models.user import UserDoc
from gaca_api.services import artifact_schema
from gaca_api.utils.auth import get_current_user

SCHEMAS = {
    "projects": {
        "fields": {
            "name": {"type": "string", "required": True, "max_length": 20},
            "effort": {"type": "integer", "min": 1, "max": 5},
            "lastTouched": {"type": "date"},
            "status": {"type": "string", "enum": ["active", "snoozed"]},
            "focus": {"type": "boolean"},
        }
    }
}


@pytest.fixture
def http(tmp_path):
    settings.db_path = str(tmp_path / "t.db")
    app.dependency_overrides[get_current_user] = lambda: UserDoc(id="u1", email="u1@example.com", display_name="Test")
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def make(http, **kw):
    r = http.post("/api/artifacts", json={"title": "Orbit", "html": "<p>x</p>", **kw})
    assert r.status_code == 201, r.text
    return r.json()


def detail(r):
    assert r.status_code == 422, r.text
    return r.json()["detail"]


# ── the checker itself ───────────────────────────────────────────────────────
def test_problems_types_and_ranges():
    fields = SCHEMAS["projects"]
    assert artifact_schema.problems(fields, {"name": "Build", "effort": 3, "lastTouched": "2026-10-14"}) == []
    assert artifact_schema.problems(None, {"anything": 1}) == []
    assert any("required" in p for p in artifact_schema.problems(fields, {"effort": 3}))
    assert any("required" in p for p in artifact_schema.problems(fields, {"name": None}))  # null = missing
    assert any("YYYY-MM-DD" in p for p in artifact_schema.problems(fields, {"name": "a", "lastTouched": "yesterday"}))
    assert any("YYYY-MM-DD" in p for p in artifact_schema.problems(fields, {"name": "a", "lastTouched": "2026-13-45"}))
    assert any("at most 5" in p for p in artifact_schema.problems(fields, {"name": "a", "effort": 9}))
    assert any("at least 1" in p for p in artifact_schema.problems(fields, {"name": "a", "effort": 0}))
    assert any("whole number" in p for p in artifact_schema.problems(fields, {"name": "a", "effort": 2.5}))
    assert any("whole number" in p for p in artifact_schema.problems(fields, {"name": "a", "effort": True}))  # bool is not an int
    assert any("one of" in p for p in artifact_schema.problems(fields, {"name": "a", "status": "gone"}))
    assert any("longer than 20" in p for p in artifact_schema.problems(fields, {"name": "x" * 21}))
    assert any("true or false" in p for p in artifact_schema.problems(fields, {"name": "a", "focus": "yes"}))


def test_datetime_accepts_z_and_offsets():
    schema = {"fields": {"at": {"type": "datetime"}}}
    for ok in ("2026-10-16T12:30:00Z", "2026-10-16T12:30:00+00:00", "2026-10-16"):
        assert artifact_schema.problems(schema, {"at": ok}) == [], ok
    assert artifact_schema.problems(schema, {"at": "noon"})


def test_strict_rejects_unknown_fields_but_default_allows_them():
    loose = {"fields": {"a": {"type": "string"}}}
    assert artifact_schema.problems(loose, {"a": "x", "extra": 1}) == []
    assert any("not a field" in p for p in artifact_schema.problems({**loose, "strict": True}, {"a": "x", "extra": 1}))


def test_message_caps_what_it_lists():
    schema = {"fields": {f"f{i}": {"type": "integer", "required": True} for i in range(8)}}
    with pytest.raises(Exception) as e:
        artifact_schema.check("c", "d1", schema, {})
    assert "c/d1 doesn't match its schema" in str(e.value) and "and 3 more" in str(e.value)


# ── through the API ──────────────────────────────────────────────────────────
def test_schema_is_stored_returned_and_checks_seeds(http):
    good = {"p1": {"name": "Build", "effort": 4, "lastTouched": "2026-10-14"}}
    a = make(http, schemas=SCHEMAS, collections={"projects": good})
    assert a["schemas"]["projects"]["fields"]["effort"]["max"] == 5
    assert http.get(f"/api/artifacts/{a['id']}").json()["schemas"] == a["schemas"]
    r = http.post("/api/artifacts", json={
        "title": "t", "html": "x", "schemas": SCHEMAS, "collections": {"projects": {"p1": {"effort": 3}}}})
    assert "p1" in detail(r) and "'name' is required" in detail(r)


def test_every_write_path_is_checked(http):
    a = make(http, schemas=SCHEMAS)
    base = f"/api/artifacts/{a['id']}/collections/projects"
    ok = http.post(base, json={"id": "p1", "data": {"name": "Build", "effort": 3}})
    assert ok.status_code == 201, ok.text
    # add
    assert "YYYY-MM-DD" in detail(http.post(base, json={"data": {"name": "x", "lastTouched": "yesterday"}}))
    # set (replace)
    assert "'name' is required" in detail(http.put(f"{base}/p1", json={"data": {"effort": 2}}))
    # patch: the merged document is what is checked
    assert "at most 5" in detail(http.patch(f"{base}/p1", json={"patch": {"effort": 9}}))
    assert http.patch(f"{base}/p1", json={"patch": {"effort": 5}}).status_code == 200
    # patching a required field to null removes it, which is rejected
    assert "'name' is required" in detail(http.patch(f"{base}/p1", json={"patch": {"name": None}}))
    # nothing bad was stored
    assert http.get(f"{base}/p1").json()["data"] == {"name": "Build", "effort": 5}


def test_batch_with_one_bad_document_applies_nothing(http):
    a = make(http, schemas=SCHEMAS)
    r = http.post(f"/api/artifacts/{a['id']}/batch", json={"ops": [
        {"op": "set", "collection": "projects", "id": "good", "data": {"name": "Fine"}},
        {"op": "set", "collection": "projects", "id": "bad", "data": {"name": "Bad", "effort": 99}},
    ]})
    assert "projects/bad" in detail(r)
    assert http.get(f"/api/artifacts/{a['id']}/collections/projects").json()["docs"] == []


def test_collections_without_a_schema_stay_free_form(http):
    a = make(http, schemas=SCHEMAS)
    r = http.post(f"/api/artifacts/{a['id']}/collections/notes", json={"data": {"anything": [1, {"x": 2}]}})
    assert r.status_code == 201


def test_page_calls_get_the_same_error_through_the_bridge_endpoint(http):
    a = make(http, schemas=SCHEMAS)
    r = http.post(f"/api/artifacts/{a['id']}/call", json={
        "method": "doc.add", "args": {"collection": "projects", "data": {"name": "x", "effort": "lots"}}})
    assert r.status_code == 422 and "whole number" in r.json()["detail"]


def test_setting_schemas_later_keeps_stored_data_and_reports_mismatches(http):
    a = make(http, collections={"projects": {"p1": {"name": "Ok", "effort": 2}, "p2": {"effort": 9}}})
    r = http.patch(f"/api/artifacts/{a['id']}", json={"schemas": SCHEMAS})
    assert r.status_code == 200
    out = r.json()
    assert out["schemas"]["projects"]["fields"]["name"]["required"] is True
    assert any(i.startswith("projects/p2:") for i in out["schema_issues"])
    assert not any(i.startswith("projects/p1:") for i in out["schema_issues"])
    # stored data was left alone, and later reads don't carry the issue list
    assert len(http.get(f"/api/artifacts/{a['id']}/collections/projects").json()["docs"]) == 2
    assert http.get(f"/api/artifacts/{a['id']}").json()["schema_issues"] == []
    # a PATCH without schemas leaves them in place; {} removes them
    http.patch(f"/api/artifacts/{a['id']}", json={"title": "Renamed"})
    assert http.get(f"/api/artifacts/{a['id']}").json()["schemas"]
    http.patch(f"/api/artifacts/{a['id']}", json={"schemas": {}})
    assert http.get(f"/api/artifacts/{a['id']}").json()["schemas"] == {}
    assert http.post(f"/api/artifacts/{a['id']}/collections/projects", json={"data": {"effort": 99}}).status_code == 201


def test_page_rewrite_keeps_the_schema(http):
    a = make(http, schemas=SCHEMAS)
    http.patch(f"/api/artifacts/{a['id']}", json={"html": "<p>v2</p>", "note": "rewrite"})
    assert http.get(f"/api/artifacts/{a['id']}").json()["schemas"] == a["schemas"]


@pytest.mark.parametrize("bad", [
    {"projects": {"fields": {"a": {"type": "colour"}}}},
    {"projects": {"fields": {"bad name": {"type": "string"}}}},
    {"Bad Name": {"fields": {"a": {"type": "string"}}}},
    {"projects": {"fields": {"a": {"type": "string", "surprise": 1}}}},
    {"projects": {"fields": {"a": {"type": "string"}}, "surprise": 1}},
])
def test_a_malformed_schema_is_rejected(http, bad):
    assert http.post("/api/artifacts", json={"title": "t", "html": "x", "schemas": bad}).status_code == 422
