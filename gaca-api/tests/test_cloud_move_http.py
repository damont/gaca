"""Part 8b: move a page to the cloud and bring it home. A stateful fake cloud + fake S3 (tests/cloud_backend.py)
plays the other end; the checks are on what happens HERE: the local rows, the pointer, the sealed backup, the
error bodies, and that no failure ever costs data."""
import copy
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from gaca_api.config import settings
from gaca_api.models.base import utcnow
from gaca_api.services import artifact_locks, cloud_bundle, cloud_move, cloud_transfer
from tests.cloud_backend import S3_HOST, FakeBackend
from tests.cloud_support import SERVER_TOKEN, http, link, mint, raw_db_bytes  # noqa: F401  (fixtures)


@pytest.fixture
def cloud(monkeypatch):
    backend = FakeBackend()
    monkeypatch.setattr(cloud_transfer, "transport_override", httpx.MockTransport(backend))
    monkeypatch.setattr(settings, "cloud_retry_pause_seconds", 0.0)
    return backend


@pytest.fixture
def linked(http, cloud):
    link(http, cloud)
    cloud.requests.clear()
    return http


def hdr(http, **extra):
    return {**http.session, **extra}


# ── helpers ──────────────────────────────────────────────────────────────────
def snapshot(http, artifact_id):
    """Everything local about one artifact, read straight from the database file."""
    con = sqlite3.connect(http.db)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
        if row is None:
            return None
        art = dict(row)
        art["doc"] = json.loads(art["doc"])
        art["state"] = json.loads(art["state"])
        return {
            "artifact": art,
            "versions": [dict(r) for r in con.execute(
                "SELECT version, created_at, note, html FROM artifact_versions WHERE artifact_id = ? ORDER BY version",
                (artifact_id,))],
            "docs": [{**dict(r), "data": json.loads(r["data"])} for r in con.execute(
                "SELECT collection, doc_id, version, created_at, updated_at, data FROM artifact_docs "
                "WHERE artifact_id = ? ORDER BY collection, created_at, doc_id", (artifact_id,))],
            "events": [{**dict(r), "payload": json.loads(r["payload"])} for r in con.execute(
                "SELECT seq, name, created_at, handled, payload FROM artifact_events WHERE artifact_id = ? ORDER BY seq",
                (artifact_id,))],
        }
    finally:
        con.close()


def count(http, table, where="1=1", args=()):
    con = sqlite3.connect(http.db)
    try:
        return con.execute(f"SELECT count(*) FROM {table} WHERE {where}", args).fetchone()[0]
    finally:
        con.close()


def make_page(http, *, versions=6, events=4, handled=1, title="Costco run", **extra):
    body = {
        "title": title, "html": "<h1>v1</h1>", "description": "weekly", "data_notes": "items: [{text, done}]",
        "crew": "mina", "state": {"list": ["a", "b"], "n": 1},
        "schemas": {"items": {"fields": {"text": {"type": "string", "required": True}}}},
        "collections": {
            "items": {"i1": {"text": "milk"}, "i2": {"text": "eggs"}, "v1.2": {"text": "dotted id"}},
            "log": {"l1": {"at": 1}},
        },
        **extra,
    }
    r = http.post("/api/artifacts", headers=http.session, json=body)
    assert r.status_code == 201, r.text
    aid = r.json()["id"]
    for n in range(2, versions + 1):
        assert http.patch(f"/api/artifacts/{aid}", headers=http.session,
                          json={"html": f"<h1>v{n}</h1>", "note": f"edit {n}"}).status_code == 200
    for n in range(events):
        assert http.post(f"/api/artifacts/{aid}/events", headers=http.session,
                         json={"name": f"picked{n}", "payload": {"n": n}}).status_code == 201
    if handled:
        assert http.post(f"/api/artifacts/{aid}/events/handled", headers=http.session, json={"upto": handled}
                         ).status_code == 200
    return aid


def move(http, aid, **kw):
    return http.post(f"/api/artifacts/{aid}/move-to-cloud", headers=hdr(http, **kw))


def bring(http, aid):
    return http.post(f"/api/artifacts/{aid}/bring-home", headers=http.session)


def ms(value):
    d = datetime.fromisoformat(value)
    return d.astimezone(timezone.utc).replace(microsecond=d.microsecond // 1000 * 1000).isoformat()


# ── the full round trip ──────────────────────────────────────────────────────
def test_round_trip_local_to_cloud_and_home_again_loses_nothing(linked, cloud):
    aid = make_page(linked)
    original = snapshot(linked, aid)

    r = move(linked, aid)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verified"] is True and body["resumed"] is False
    assert body["artifact"]["id"] == aid and body["artifact"]["home"] == "cloud" and body["artifact"]["role"] == "owner"
    assert body["artifact"]["cloud_status"] == "active" and body["artifact"]["moving"] is False
    # local rows are gone, a pointer and a sealed backup exist; the cloud copy has the SAME id
    assert snapshot(linked, aid) is None
    assert count(linked, "cloud_pointers", "id = ?", (aid,)) == 1
    assert count(linked, "artifact_cloud_backups", "artifact_id = ?", (aid,)) == 1
    g = cloud.gacabits[aid]
    assert g["title"] == "Costco run" and g["state"] == original["artifact"]["state"] and g["status"] == "active"
    # the cloud got the 5 newest page versions (the current one + 4 older), only the unhandled events
    assert [v["version"] for v in g["versions"]] == [2, 3, 4, 5, 6]  # the 5 newest: the current page + 4 older
    assert [e["name"] for e in g["events"]] == ["picked1", "picked2", "picked3"]
    # the list shows it as a cloud page; reads are forwarded
    listed = {a["id"]: a for a in linked.get("/api/artifacts", headers=linked.session).json()["artifacts"]}
    assert listed[aid]["home"] == "cloud"
    assert linked.get(f"/api/artifacts/{aid}/state", headers=linked.session).status_code in (200, 404)

    r = bring(linked, aid)
    assert r.status_code == 200, r.text
    assert r.json()["confirmed"] is True and r.json()["artifact"]["home"] == "local"
    back = snapshot(linked, aid)
    assert count(linked, "cloud_pointers", "id = ?", (aid,)) == 0
    assert cloud.gacabits[aid]["confirmed"] is True

    a0, a1 = original["artifact"], back["artifact"]
    assert (a1["html"], a1["version"], a1["state"], a1["state_version"], a1["archived"]) == (
        a0["html"], a0["version"], a0["state"], a0["state_version"], a0["archived"])
    for key in ("title", "description", "data_notes", "schemas", "crew"):
        assert a1["doc"][key] == a0["doc"][key], key
    assert ms(a1["doc"]["created_at"]) == ms(a0["doc"]["created_at"])
    # documents: same ids, data, and timestamps (to the millisecond: the cloud's precision)
    key = lambda d: (d["collection"], d["doc_id"])  # noqa: E731
    assert [(key(d), d["data"], ms(d["created_at"]), ms(d["updated_at"])) for d in back["docs"]] == [
        (key(d), d["data"], ms(d["created_at"]), ms(d["updated_at"])) for d in original["docs"]]
    # only unhandled events came back, in order, with their payloads and times
    assert [(e["name"], e["payload"], ms(e["created_at"]), e["handled"]) for e in back["events"]] == [
        (e["name"], e["payload"], ms(e["created_at"]), 0) for e in original["events"] if not e["handled"]]
    # the 4 newest page versions the cloud kept, plus the current one, with their html and notes
    kept = [v for v in original["versions"] if v["version"] >= 2]
    assert [(v["version"], v["html"]) for v in back["versions"]] == [(v["version"], v["html"]) for v in kept]
    assert [v["note"] for v in back["versions"][:-1]] == [v["note"] for v in kept[:-1]]
    # the rev the page polls with moved FORWARD (never collides with one it saw)
    assert a1["rev"] > 0
    # the page works again
    assert linked.patch(f"/api/artifacts/{aid}/state", headers=linked.session, json={"patch": {"n": 2}}).status_code == 200
    got = linked.get(f"/api/artifacts/{aid}", headers=linked.session).json()
    assert got["home"] == "local" and got["moving"] is False and got["cloud_status"] is None


def test_the_cloud_is_only_ever_told_what_the_contract_allows(linked, cloud):
    aid = make_page(linked)
    assert move(linked, aid).status_code == 200
    up = json.loads(cloud.seen("POST", "/uploads")[0].content)
    assert up["purpose"] == "bundle" and up["size_bytes"] > 0
    put = cloud.s3_requests[0]
    assert put.method == "PUT" and put.headers["content-encoding"] == "gzip" and put.headers["content-type"] == "application/json"
    assert "authorization" not in put.headers  # the bucket never sees the cloud token
    bundle = cloud_bundle.unpack(put.content)
    assert bundle["kind"] == "full" and bundle["meta"]["origin_id"] == aid and cloud_bundle.validate_bundle(bundle) == []
    assert json.loads(cloud.seen("POST", "/gacabits/import")[0].content) == {"upload_id": next(iter(cloud.uploads))}


def test_nothing_secret_is_logged_or_answered(linked, cloud, caplog):
    caplog.set_level(logging.DEBUG)
    aid = make_page(linked)
    cloud.faults["s3_put"] = ["network"]  # also exercise the failure log lines
    r = move(linked, aid)
    assert r.status_code == 200
    r2 = bring(linked, aid)
    seen = r.text + r2.text + caplog.text
    for secret in (SERVER_TOKEN, "SECRETSIG", "amazonaws.com", "X-Amz"):
        assert secret not in seen, secret


# ── preflight: too big, wrong state ──────────────────────────────────────────
def test_too_big_for_the_cloud_is_refused_before_anything_is_sent(linked, cloud):
    aid = make_page(linked, versions=1, events=0, handled=0)
    blob = "x" * 190_000
    for n in range(70):  # 70 documents of ~190 KB = ~13 MB, each one fine on its own
        assert linked.post(f"/api/artifacts/{aid}/collections/big", headers=linked.session,
                           json={"data": {"blob": blob}, "id": f"d{n}"}).status_code == 201
    before = snapshot(linked, aid)
    r = move(linked, aid)
    assert r.status_code == 413
    body = r.json()
    assert body["code"] == "too_large_for_cloud" and body["detail"]["phase"] == "preflight"
    whole = next(p for p in body["detail"]["problems"] if p["item"] == "the whole gacabit")
    assert whole["size"] > whole["limit"] == 12 * 1024 * 1024
    assert whole["biggest"][0]["item"] == "collection 'big'"
    assert cloud.requests == [] and cloud.s3_requests == []  # NOTHING was sent, not even a ping
    assert snapshot(linked, aid) == before


def test_too_many_unhandled_events_are_listed(linked, cloud):
    aid = make_page(linked, versions=1, events=0, handled=0)
    for n in range(201):
        linked.post(f"/api/artifacts/{aid}/events", headers=linked.session, json={"name": "e", "payload": {"n": n}})
    r = move(linked, aid)
    assert r.status_code == 413
    assert {"item": "unhandled events (mark some handled)", "size": 201, "limit": 200, "unit": "events"} in r.json()["detail"]["problems"]
    assert cloud.requests == []


def test_archived_unknown_and_already_cloud_pages_are_refused(linked, cloud):
    aid = make_page(linked)
    linked.patch(f"/api/artifacts/{aid}", headers=linked.session, json={"archived": True})
    r = move(linked, aid)
    assert r.status_code == 409 and r.json()["code"] == "artifact_archived"
    assert move(linked, "0" * 32).status_code == 404
    linked.patch(f"/api/artifacts/{aid}", headers=linked.session, json={"archived": False})
    assert move(linked, aid).status_code == 200
    again = move(linked, aid)
    assert again.status_code == 409 and again.json()["code"] == "already_in_cloud"


def test_not_linked_or_read_only_link_refuses(http, cloud):
    aid = make_page(http)
    r = move(http, aid)
    assert r.status_code == 409 and r.json()["code"] == "cloud_not_linked"
    link(http, cloud, scope="read")
    con = sqlite3.connect(http.db)
    con.execute("UPDATE cloud_link SET scope = 'read'")
    con.commit()
    con.close()
    r = move(http, aid)
    assert r.status_code == 403 and r.json()["code"] == "cloud_link_read_only"
    assert snapshot(http, aid) is not None


def test_an_id_the_cloud_cannot_keep_is_refused(linked, cloud, monkeypatch):
    aid = make_page(linked)
    monkeypatch.setattr(cloud_bundle, "portable_id", lambda _id: False)
    r = move(linked, aid)
    assert r.status_code == 422 and r.json()["code"] == "id_not_portable" and cloud.requests == []


# ── every failure leaves the local page exactly as it was ────────────────────
FAILURES = [
    # (name, how to break the cloud, expected status, expected code, phase)
    ("uploads_down", lambda c: c.faults.update(uploads=[(503, "billing_unavailable", "x")]), 503, "billing_unavailable", "uploading"),
    ("uploads_413", lambda c: c.faults.update(uploads=[(413, "payload_too_large", "big")]), 413, "too_large_for_cloud", "uploading"),
    ("s3_put_403", lambda c: c.faults.update(s3_put=[403]), 502, "transfer_failed", "uploading"),
    ("s3_put_network", lambda c: c.faults.update(s3_put=["network", "network", "network"]), 502, "transfer_failed", "uploading"),
    ("import_402", lambda c: c.faults.update(**{"import": [(402, "billing_required", "set up billing")]}), 402, "billing_required", "importing"),
    ("import_id_conflict", lambda c: c.foreign_ids.add("@AID@"), 409, "id_conflict", "importing"),
    ("import_413", lambda c: c.faults.update(**{"import": [(413, "gacabit_size_exceeded", "too big")]}), 413, "too_large_for_cloud", "importing"),
    ("import_422_blocked", lambda c: c.faults.update(**{"import": [(422, "validation_blocked", "blocked", {"findings": [{"rule": "html.eval", "severity": "block", "message": "eval"}]})]}), 422, "validation_blocked", "importing"),
    ("import_429", lambda c: c.faults.update(**{"import": [(429, "rate_limited", "slow down", None, {"Retry-After": "120"})]}), 429, "rate_limited", "importing"),
    ("import_503_forever", lambda c: c.faults.update(**{"import": [(503, "validation_unavailable", "later")] * 9}), 503, "validation_unavailable", "importing"),
    ("import_403", lambda c: c.faults.update(**{"import": [(403, "forbidden_scope", "this server token is read-only")]}), 403, "forbidden_scope", "importing"),
    ("cloud_down", lambda c: setattr(c, "down", True), 502, "cloud_unavailable", "preflight"),
    ("verify_mismatch", lambda c: c.faults.update(data_tamper=[1]), 502, "verify_failed", "verifying"),
]


@pytest.mark.parametrize("name,setup,status,code,phase", FAILURES, ids=[f[0] for f in FAILURES])
def test_a_failed_move_changes_nothing_here(linked, cloud, name, setup, status, code, phase):
    aid = make_page(linked)
    before = snapshot(linked, aid)
    setup(cloud)
    cloud.foreign_ids = {i.replace("@AID@", aid) for i in cloud.foreign_ids}
    r = move(linked, aid)
    assert r.status_code == status, r.text
    body = r.json()
    assert body["code"] == code and isinstance(body["message"], str) and isinstance(body["detail"], dict)
    assert body["detail"]["phase"] == phase and body["detail"]["local_data_changed"] is False
    assert snapshot(linked, aid) == before  # every row, byte for byte
    assert count(linked, "cloud_pointers") == 0 and count(linked, "artifact_cloud_backups") == 0
    # the app is told, and the page is writable again
    got = linked.get(f"/api/artifacts/{aid}", headers=linked.session).json()
    assert got["cloud_status"] == "move_failed" and got["moving"] is False and got["home"] == "local"
    assert linked.patch(f"/api/artifacts/{aid}/state", headers=linked.session, json={"patch": {"k": 1}}).status_code == 200


def test_429_keeps_the_retry_after_header(linked, cloud):
    aid = make_page(linked)
    cloud.faults["import"] = [(429, "rate_limited", "slow down", None, {"Retry-After": "120"})]
    r = move(linked, aid)
    assert r.status_code == 429 and r.headers["retry-after"] == "120"


def test_a_503_on_the_import_is_retried_with_the_same_upload(linked, cloud):
    aid = make_page(linked)
    cloud.faults["import"] = [(503, "validation_unavailable", "later")] * 2
    r = move(linked, aid)
    assert r.status_code == 200
    assert len(cloud.seen("POST", "/uploads")) == 1  # one upload, imported on the third try
    assert len(cloud.seen("POST", "/gacabits/import")) == 3 and cloud.imports == 1


def test_a_lost_import_answer_continues_instead_of_failing(linked, cloud):
    aid = make_page(linked)
    cloud.faults["import"] = ["lose"]  # the cloud imports, then the answer never arrives
    r = move(linked, aid)
    assert r.status_code == 200, r.text
    assert cloud.imports == 1 and len(cloud.seen("POST", "/uploads")) == 1  # NOT imported twice
    assert snapshot(linked, aid) is None and count(linked, "cloud_pointers") == 1


def test_a_lost_answer_and_an_unreachable_cloud_still_ends_in_success(linked, cloud):
    """The import worked, its answer was lost, the follow-up question also failed: the retry finds the upload
    already used (404) and the gacabit present, and carries on."""
    aid = make_page(linked)
    cloud.faults["import"] = ["lose"]
    cloud.faults["read_after_import"] = ["network"]
    r = move(linked, aid)
    assert r.status_code == 200, r.text
    assert cloud.imports == 1 and len(cloud.seen("POST", "/uploads")) == 1 and len(cloud.seen("POST", "/gacabits/import")) == 2


def test_a_crash_between_import_and_conversion_is_continued_on_retry(linked, cloud, monkeypatch):
    aid = make_page(linked)
    before = snapshot(linked, aid)
    from gaca_api.db import cloud_move as move_db

    real = move_db._upsert_pointer

    def boom(raw, pointer):
        raise RuntimeError("power cut")

    monkeypatch.setattr(move_db, "_upsert_pointer", boom)
    r = move(linked, aid)
    assert r.status_code == 500 and r.json()["code"] == "move_failed" and r.json()["detail"]["phase"] == "converting"
    # the transaction rolled back as a whole: no backup row, no pointer, every local row intact
    assert snapshot(linked, aid) == before
    assert count(linked, "artifact_cloud_backups") == 0 and count(linked, "cloud_pointers") == 0
    assert cloud.imports == 1  # but the cloud copy exists

    monkeypatch.setattr(move_db, "_upsert_pointer", real)
    r = move(linked, aid)
    assert r.status_code == 200 and r.json()["resumed"] is True
    assert cloud.imports == 1 and len(cloud.seen("POST", "/uploads")) == 1  # continued; nothing uploaded again
    assert snapshot(linked, aid) is None and count(linked, "artifact_cloud_backups") == 1


def test_a_cloud_copy_that_differs_is_never_adopted(linked, cloud, monkeypatch):
    aid = make_page(linked)
    from gaca_api.db import cloud_move as move_db

    real = move_db._upsert_pointer
    monkeypatch.setattr(move_db, "_upsert_pointer", lambda raw, p: (_ for _ in ()).throw(RuntimeError("x")))
    assert move(linked, aid).status_code == 500
    monkeypatch.setattr(move_db, "_upsert_pointer", real)
    # the person edits the page here after the failed try; the cloud copy is now stale
    assert linked.patch(f"/api/artifacts/{aid}/state", headers=linked.session, json={"patch": {"n": 99}}).status_code == 200
    before = snapshot(linked, aid)
    r = move(linked, aid)
    assert r.status_code == 409 and r.json()["code"] == "cloud_copy_differs"
    assert "state" in r.json()["detail"]["mismatches"]
    assert snapshot(linked, aid) == before and count(linked, "cloud_pointers") == 0


def test_a_page_edited_during_the_move_is_not_converted(linked, cloud, monkeypatch):
    aid = make_page(linked)
    real = cloud_move._verify

    async def verify_then_somebody_writes(*a, **kw):
        out = await real(*a, **kw)
        con = sqlite3.connect(linked.db)
        con.execute("UPDATE artifacts SET state = '{\"sneaky\": true}', rev = rev + 1 WHERE id = ?", (aid,))
        con.commit()
        con.close()
        return out

    monkeypatch.setattr(cloud_move, "_verify", verify_then_somebody_writes)
    r = move(linked, aid)
    assert r.status_code == 409 and r.json()["code"] == "changed_during_move"
    assert snapshot(linked, aid)["artifact"]["state"] == {"sneaky": True}  # the late write is still there
    assert count(linked, "cloud_pointers") == 0 and count(linked, "artifact_cloud_backups") == 0


def test_the_page_is_read_only_while_it_moves(linked, cloud):
    aid = make_page(linked)
    artifact_locks.MOVING.add(aid)
    try:
        for method, suffix, body in [
            ("PATCH", "", {"title": "x"}), ("PUT", "/state", {"state": {}}), ("PATCH", "/state", {"patch": {"a": 1}}),
            ("POST", "/collections/items", {"data": {"t": 1}}), ("PUT", "/collections/items/i1", {"data": {"text": "z"}}),
            ("DELETE", "/collections/items/i1", None), ("DELETE", "/collections/items", None),
            ("POST", "/batch", {"ops": [{"op": "clear", "collection": "items"}]}),
            ("POST", "/events", {"name": "x"}), ("POST", "/events/handled", {"upto": 99}), ("DELETE", "", None),
        ]:
            r = linked.request(method, f"/api/artifacts/{aid}{suffix}", headers=linked.session, json=body)
            assert r.status_code == 423 and r.json()["code"] == "artifact_moving", (method, suffix, r.text)
        assert linked.get(f"/api/artifacts/{aid}/data", headers=linked.session).status_code == 200  # reads are fine
        got = linked.get(f"/api/artifacts/{aid}", headers=linked.session).json()
        assert got["moving"] is True and got["cloud_status"] == "moving"
        listed = [a for a in linked.get("/api/artifacts", headers=linked.session).json()["artifacts"] if a["id"] == aid]
        assert listed[0]["moving"] is True
    finally:
        artifact_locks.MOVING.discard(aid)
    assert linked.patch(f"/api/artifacts/{aid}", headers=linked.session, json={"title": "ok"}).status_code == 200


def test_only_one_transfer_per_page_at_a_time(linked, cloud):
    aid = make_page(linked)
    artifact_locks.BUSY.add(aid)
    try:
        r = move(linked, aid)
        assert r.status_code == 409 and r.json()["code"] == "transfer_in_progress"
        assert bring(linked, aid).status_code == 409
    finally:
        artifact_locks.BUSY.discard(aid)


# ── the presigned URL: where this server will and will not connect ────────────
BAD_HOSTS = [
    ("http_scheme", dict(s3_url_scheme="http")),
    ("other_host", dict(s3_url_host="evil.example.com")),
    ("suffix_trick", dict(s3_url_host="bucket.s3.amazonaws.com.evil.example.com")),
    ("userinfo_trick", dict(s3_url_suffix="@evil.example.com")),
    ("not_s3", dict(s3_url_host="ec2.amazonaws.com")),
    ("bare_domain", dict(s3_url_host="amazonaws.com")),
    ("lookalike", dict(s3_url_host="bucket.s3.amazonaws.com.cn")),
    ("lookalike2", dict(s3_url_host="evils3.amazonaws.com")),
    ("ip_address", dict(s3_url_host="169.254.169.254")),
    ("localhost", dict(s3_url_host="localhost")),
    ("port", dict(s3_url_host="bucket.s3.amazonaws.com:8443")),
    ("backslash", dict(s3_url_host="evil.example.com\\@bucket.s3.amazonaws.com")),
]


@pytest.mark.parametrize("name,fields", BAD_HOSTS, ids=[b[0] for b in BAD_HOSTS])
def test_an_upload_address_that_is_not_s3_is_never_contacted(linked, cloud, name, fields):
    aid = make_page(linked)
    before = snapshot(linked, aid)
    for k, v in fields.items():
        setattr(cloud, k, v)
    r = move(linked, aid)
    assert r.status_code == 502 and r.json()["code"] == "transfer_url_rejected", r.text
    assert cloud.s3_requests == [] and cloud.other_hosts == []  # no connection was even attempted
    assert snapshot(linked, aid) == before and cloud.imports == 0
    assert "evil" not in r.text and "amazonaws" not in r.text  # the address is not echoed


def test_a_redirect_from_the_bucket_is_not_followed(linked, cloud):
    aid = make_page(linked)
    cloud.faults["s3_redirect"] = [True]
    r = move(linked, aid)
    assert r.status_code == 502 and r.json()["code"] == "transfer_failed"
    assert cloud.other_hosts == [] and snapshot(linked, aid) is not None


@pytest.mark.parametrize("headers", [{"Authorization": "Bearer x"}, {"Host": "evil.example.com"}, {"Cookie": "a=b"},
                                    {"Content-Type": "a\r\nX-Evil: 1"}])
def test_upload_headers_the_presign_names_must_be_harmless(linked, cloud, headers):
    aid = make_page(linked)
    cloud.presign_headers = {"Content-Type": "application/json", "Content-Encoding": "gzip", **headers}
    r = move(linked, aid)
    assert r.status_code == 502 and r.json()["code"] == "transfer_url_rejected" and cloud.s3_requests == []


def test_extra_amz_headers_the_presign_requires_are_sent(linked, cloud):
    aid = make_page(linked)
    cloud.presign_headers = {"Content-Type": "application/json", "Content-Encoding": "gzip",
                             "x-amz-server-side-encryption": "aws:kms"}
    assert move(linked, aid).status_code == 200
    assert cloud.s3_requests[0].headers["x-amz-server-side-encryption"] == "aws:kms"


@pytest.mark.parametrize("url", [
    "https://bucket.s3.amazonaws.com/k?X-Amz-Signature=a",
    "https://bucket.s3.eu-west-1.amazonaws.com/k",
    "https://bucket.s3-eu-west-1.amazonaws.com/k",
    "https://my.bucket.name.s3.dualstack.us-east-2.amazonaws.com/k",
    "https://s3.amazonaws.com/bucket/k",
    "https://s3.us-east-1.amazonaws.com/bucket/k",
    "https://s3-us-west-2.amazonaws.com/bucket/k",
    "https://bucket.s3.amazonaws.com:443/k",
])
def test_s3_addresses_that_are_accepted(url):
    assert cloud_transfer.check_url(url) == url


@pytest.mark.parametrize("url", [
    "http://bucket.s3.amazonaws.com/k", "ftp://bucket.s3.amazonaws.com/k", "https://x.amazonaws.com@evil.com/k",
    "https://bucket.s3.amazonaws.com@evil.com/k", "https://user:pw@bucket.s3.amazonaws.com/k",
    "https://evil.com/bucket.s3.amazonaws.com", "https://evil.com#@bucket.s3.amazonaws.com/",
    "https://evil.com\\@bucket.s3.amazonaws.com/", "https://bucket.s3.amazonaws.com.evil.com/k",
    "https://s3.amazonaws.com.evil.com/k", "https://xs3.amazonaws.com/k", "https://bucket.s3.amazonaws.com:8080/k",
    "https://127.0.0.1/k", "https://[::1]/k", "https://localhost/k", "https://bucket.s3.amazonaws.com/ k",
    "https://bucket.s3.amazonaws.com/k\n", "https://bucket.s3.amazonaws.com。evil.com/k", "//bucket.s3.amazonaws.com/k",
    "/api/v1/storage/local?key=x", "", None, 5, "https://.s3.amazonaws.com/k", "https://bucket..s3.amazonaws.com/k",
    "https://bucket.s3.amazonaws.com/k#frag", "https://BUCKET.S3.AMAZONAWS.COM.evil.com/k",
])
def test_addresses_that_are_not_accepted(url):
    from gaca_api.services.exceptions import CloudError

    with pytest.raises(CloudError) as e:
        cloud_transfer.check_url(url)
    assert e.value.code == "transfer_url_rejected"


# ── bring home ───────────────────────────────────────────────────────────────
def moved(linked, cloud, **kw):
    aid = make_page(linked, **kw)
    assert move(linked, aid).status_code == 200
    cloud.requests.clear()
    cloud.s3_requests.clear()
    return aid


def pointer_intact(linked, aid):
    return count(linked, "cloud_pointers", "id = ?", (aid,)) == 1 and snapshot(linked, aid) is None


BRING_FAILURES = [
    ("download_404", lambda c: c.faults.update(export_vanish=[1]), "transfer_failed"),
    ("corrupt_gzip", lambda c: c.faults.update(bring_home=["corrupt"]), "bundle_invalid"),
    ("not_https", lambda c: setattr(c, "s3_url_scheme", "http"), "transfer_url_rejected"),
    ("other_host", lambda c: setattr(c, "s3_url_host", "evil.example.com"), "transfer_url_rejected"),
    ("redirect", lambda c: c.faults.update(s3_redirect=[True]), "transfer_failed"),
]


@pytest.mark.parametrize("name,setup,code", BRING_FAILURES, ids=[b[0] for b in BRING_FAILURES])
def test_a_failed_bring_home_leaves_the_pointer_and_puts_the_cloud_copy_back(linked, cloud, name, setup, code):
    aid = moved(linked, cloud)
    setup(cloud)
    r = bring(linked, aid)
    assert r.status_code >= 400, r.text
    body = r.json()
    assert set(body) == {"code", "message", "detail"} and body["detail"]["cloud_state"] == "restored"
    if code:
        assert body["code"] == code
    assert pointer_intact(linked, aid)
    assert cloud.gacabits[aid]["status"] == "active"  # undone: the cloud copy is as it was
    assert cloud.other_hosts == []
    # a retry after fixing the problem works
    cloud.faults.clear()
    cloud.s3_url_scheme, cloud.s3_url_host = "https", S3_HOST
    r = bring(linked, aid)
    assert r.status_code == 200 and r.json()["confirmed"] is True
    assert snapshot(linked, aid) is not None and count(linked, "cloud_pointers") == 0


BAD_BUNDLES = {
    "template_kind": lambda b: {**b, "kind": "template"},
    "extra_key": lambda b: {**b, "surprise": 1},
    "wrong_version": lambda b: {**b, "bundle_version": 2},
    "bad_doc_name": lambda b: {**b, "collections": {"BAD NAME": {"a": {"data": {}, "created_at": "2026-01-01T00:00:00Z"}}}},
    "missing_html": lambda b: {k: v for k, v in b.items() if k != "html"},
    "doc_without_time": lambda b: {**b, "collections": {"x": {"a": {"data": {}}}}},
    "huge_doc": lambda b: {**b, "collections": {"x": {"a": {"data": {"s": "x" * 250_000}, "created_at": "2026-01-01T00:00:00Z"}}}},
    "bad_time": lambda b: {**b, "collections": {"x": {"a": {"data": {}, "created_at": "yesterday"}}}},
}


@pytest.mark.parametrize("name", list(BAD_BUNDLES))
def test_a_bundle_that_does_not_validate_is_never_restored(linked, cloud, name):
    import gzip as gz

    aid = moved(linked, cloud)
    real = cloud.export_bundle
    cloud.export_bundle = lambda g: BAD_BUNDLES[name](real(g))
    r = bring(linked, aid)
    assert r.status_code == 502 and r.json()["code"] == "bundle_invalid", r.text
    assert pointer_intact(linked, aid) and cloud.gacabits[aid]["status"] == "active"
    del gz


def test_a_gzip_bomb_is_stopped_at_the_cap(linked, cloud, monkeypatch):
    import gzip as gz

    aid = moved(linked, cloud)
    cloud.export_bundle = lambda g: {"padding": "0" * (20 * 1024 * 1024)}  # a few KB of gzip, 20 MB inflated
    r = bring(linked, aid)
    assert r.status_code == 502 and r.json()["code"] == "bundle_invalid"
    assert pointer_intact(linked, aid)
    assert len(gz.compress(b"0" * (20 * 1024 * 1024))) < 100_000  # it really was tiny on the wire


def test_nan_in_a_bundle_is_refused(linked, cloud):
    aid = moved(linked, cloud)
    real = cloud.export_bundle

    def with_nan(g):
        b = real(g)
        b["state"] = {"x": float("nan")}
        return b

    cloud.export_bundle = with_nan
    r = bring(linked, aid)
    assert r.status_code == 502 and r.json()["code"] == "bundle_invalid" and pointer_intact(linked, aid)


def test_a_bring_home_the_cloud_will_not_do_is_passed_on(linked, cloud):
    aid = moved(linked, cloud)
    cloud.faults["bring_home"] = [(403, "forbidden_role", "only the owner can bring a gacabit home")]
    r = bring(linked, aid)
    assert r.status_code == 403 and r.json()["code"] == "forbidden_role" and pointer_intact(linked, aid)
    assert bring(linked, "f" * 32).status_code == 404
    assert pointer_intact(linked, aid)


def test_the_cloud_is_told_the_page_arrived_only_after_the_local_copy_exists(linked, cloud):
    aid = moved(linked, cloud)
    at_confirm = {}

    def spy():
        at_confirm["local"] = snapshot(linked, aid) is not None
        at_confirm["pointer"] = count(linked, "cloud_pointers", "id = ?", (aid,))

    cloud.on_confirm = spy
    assert bring(linked, aid).status_code == 200
    assert at_confirm == {"local": True, "pointer": 0}


def test_a_lost_confirm_leaves_the_page_home_and_is_retried(linked, cloud):
    aid = moved(linked, cloud)
    cloud.faults["confirm"] = ["network"]
    r = bring(linked, aid)
    assert r.status_code == 200 and r.json()["confirmed"] is False and "told" in r.json()["warning"]
    assert snapshot(linked, aid) is not None and count(linked, "cloud_pointers") == 0
    got = linked.get(f"/api/artifacts/{aid}", headers=linked.session).json()
    assert got["cloud_status"] == "bring_home_unconfirmed"
    assert cloud.gacabits[aid]["confirmed"] is False
    again = bring(linked, aid)  # retry: only the confirm is repeated
    assert again.status_code == 200 and again.json()["confirmed"] is True and cloud.gacabits[aid]["confirmed"] is True
    assert len(cloud.seen("POST", f"/gacabits/{aid}/bring-home")) == 1
    assert linked.get(f"/api/artifacts/{aid}", headers=linked.session).json()["cloud_status"] is None


def test_maintenance_retries_an_unsent_confirm(linked, cloud):
    aid = moved(linked, cloud)
    cloud.faults["confirm"] = ["network"]
    assert bring(linked, aid).json()["confirmed"] is False
    done = linked.portal.call(cloud_move.maintenance)
    assert done["confirms_sent"] == 1 and cloud.gacabits[aid]["confirmed"] is True
    assert linked.get(f"/api/artifacts/{aid}", headers=linked.session).json()["cloud_status"] is None


def test_an_unfinished_earlier_bring_home_is_undone_and_redone(linked, cloud):
    aid = moved(linked, cloud)
    cloud.faults["bring_home"] = ["corrupt"]
    cloud.faults["undo_closed"] = [1]  # the automatic undo is refused: the cloud copy stays locked
    r = bring(linked, aid)
    assert r.status_code == 502 and r.json()["detail"]["cloud_state"] == "brought_home"
    assert cloud.gacabits[aid]["status"] == "brought_home" and pointer_intact(linked, aid)
    cloud.faults.clear()
    r = bring(linked, aid)  # the cloud now answers "already brought home": undo it, then go again
    assert r.status_code == 200 and snapshot(linked, aid) is not None


def test_bringing_home_a_page_that_is_already_local_is_a_plain_answer(linked, cloud):
    aid = make_page(linked)
    r = bring(linked, aid)
    assert r.status_code == 409 and r.json()["code"] == "not_cloud_homed"


def test_bring_home_uses_the_backup_only_for_what_a_bundle_cannot_carry(linked, cloud):
    aid = make_page(linked, crew="mina")
    session_alert = {"session_id": None, "alert_id": None}
    assert move(linked, aid).status_code == 200
    # the cloud copy was changed meanwhile (title, state): the CLOUD's data wins, the backup supplies only crew
    g = cloud.gacabits[aid]
    g["title"], g["state"] = "Renamed in the cloud", {"cloud": "wins"}
    assert bring(linked, aid).status_code == 200
    back = snapshot(linked, aid)
    assert back["artifact"]["doc"]["title"] == "Renamed in the cloud" and back["artifact"]["state"] == {"cloud": "wins"}
    assert back["artifact"]["doc"]["crew"] == "mina" and session_alert["alert_id"] is None


def test_without_a_backup_everything_the_cloud_sent_still_comes_home(linked, cloud):
    aid = make_page(linked)
    assert move(linked, aid).status_code == 200
    con = sqlite3.connect(linked.db)
    con.execute("DELETE FROM artifact_cloud_backups")
    con.commit()
    con.close()
    assert bring(linked, aid).status_code == 200
    back = snapshot(linked, aid)
    assert back["artifact"]["doc"]["title"] == "Costco run" and len(back["docs"]) == 4 and back["artifact"]["doc"]["crew"] is None


# ── undo ─────────────────────────────────────────────────────────────────────
def undo(http, aid):
    return http.post(f"/api/artifacts/{aid}/undo-bring-home", headers=http.session)


def test_undo_returns_the_page_to_the_cloud(linked, cloud):
    aid = moved(linked, cloud)
    assert bring(linked, aid).status_code == 200
    r = undo(linked, aid)
    assert r.status_code == 200 and r.json()["home"] == "cloud" and r.json()["undone"] is True, r.text
    assert cloud.gacabits[aid]["status"] == "active"
    assert pointer_intact(linked, aid)
    assert count(linked, "artifact_bring_homes") == 0 and count(linked, "artifact_cloud_backups", "artifact_id = ?", (aid,)) == 2


def test_undo_is_refused_when_the_page_was_edited_since_it_came_home(linked, cloud):
    aid = moved(linked, cloud)
    assert bring(linked, aid).status_code == 200
    assert linked.patch(f"/api/artifacts/{aid}/state", headers=linked.session, json={"patch": {"mine": 1}}).status_code == 200
    before = snapshot(linked, aid)
    r = undo(linked, aid)
    assert r.status_code == 409 and r.json()["code"] == "local_changed_since_bring_home"
    assert snapshot(linked, aid) == before and cloud.gacabits[aid]["status"] == "brought_home"  # cloud untouched
    assert cloud.seen("POST", f"/gacabits/{aid}/bring-home/undo") == []


def test_undo_after_the_window_is_the_clouds_410(linked, cloud):
    aid = moved(linked, cloud)
    assert bring(linked, aid).status_code == 200
    cloud.faults["undo_closed"] = [1]
    r = undo(linked, aid)
    assert r.status_code == 410 and r.json()["code"] == "undo_window_closed" and snapshot(linked, aid) is not None


def test_undo_on_a_cloud_pointer_is_a_pass_through(linked, cloud):
    aid = moved(linked, cloud)
    cloud.faults["bring_home"] = ["corrupt"]
    cloud.faults["undo_closed"] = [1]
    assert bring(linked, aid).status_code == 502  # leaves the cloud copy locked, pointer intact
    cloud.faults.clear()
    r = undo(linked, aid)
    assert r.status_code == 200 and r.json()["home"] == "cloud" and cloud.gacabits[aid]["status"] == "active"
    assert undo(linked, "e" * 32).status_code == 409


# ── the sealed backup ────────────────────────────────────────────────────────
def test_a_backup_is_kept_for_30_days_then_cleaned_up(linked, cloud):
    aid = moved(linked, cloud)
    con = sqlite3.connect(linked.db)
    created, expires, blob = con.execute(
        "SELECT created_at, expires_at, snapshot FROM artifact_cloud_backups WHERE artifact_id = ?", (aid,)).fetchone()
    con.close()
    assert datetime.fromisoformat(expires) - datetime.fromisoformat(created) == timedelta(days=30)
    assert blob[:2] == b"\x1f\x8b"  # gzip
    portal = linked.portal
    assert portal.call(cloud_move.maintenance, utcnow() + timedelta(days=29))["backups_removed"] == 0
    assert count(linked, "artifact_cloud_backups") == 1
    assert portal.call(cloud_move.maintenance, utcnow() + timedelta(days=31))["backups_removed"] == 1
    assert count(linked, "artifact_cloud_backups") == 0
    assert count(linked, "cloud_pointers", "id = ?", (aid,)) == 1  # the pointer is not the backup's business


def test_restore_backup_cli_puts_the_page_back_exactly(linked, cloud, tmp_path):
    import os
    import subprocess
    import sys

    aid = make_page(linked)
    original = snapshot(linked, aid)
    assert move(linked, aid).status_code == 200
    assert snapshot(linked, aid) is None
    env = {**os.environ, "GACA_DB_PATH": linked.db}
    listed = subprocess.run([sys.executable, "-m", "gaca_api.cli", "cloud", "list-backups"], env=env,
                            capture_output=True, text=True, cwd=os.getcwd())
    assert listed.returncode == 0 and aid in listed.stdout, listed.stderr
    done = subprocess.run([sys.executable, "-m", "gaca_api.cli", "cloud", "restore-backup", aid], env=env,
                          capture_output=True, text=True, cwd=os.getcwd())
    assert done.returncode == 0, done.stderr
    assert "Restored page" in done.stdout and "cloud was not touched" in done.stdout
    assert snapshot(linked, aid) == original  # every row, event sequence numbers and document versions included
    assert count(linked, "cloud_pointers") == 0
    again = subprocess.run([sys.executable, "-m", "gaca_api.cli", "cloud", "restore-backup", aid], env=env,
                           capture_output=True, text=True, cwd=os.getcwd())
    assert again.returncode == 1 and "already exists" in again.stderr  # never overwrites a local page
    nothing = subprocess.run([sys.executable, "-m", "gaca_api.cli", "cloud", "restore-backup", "nope"], env=env,
                             capture_output=True, text=True, cwd=os.getcwd())
    assert nothing.returncode == 1 and "No backup" in nothing.stderr


def test_a_damaged_backup_is_not_restored(linked, cloud):
    aid = moved(linked, cloud)
    con = sqlite3.connect(linked.db)
    con.execute("UPDATE artifact_cloud_backups SET snapshot = ?", (b"corrupted",))
    con.commit()
    con.close()
    portal = linked.portal
    with pytest.raises(Exception) as e:
        portal.call(cloud_move.restore_backup, aid)
    assert getattr(e.value, "code", "") == "backup_damaged"


# ── unlink ───────────────────────────────────────────────────────────────────
def test_unlink_is_refused_while_pages_live_in_the_cloud(linked, cloud):
    aid = moved(linked, cloud)
    r = linked.delete("/api/cloud/link", headers=linked.session)
    assert r.status_code == 409 and r.json()["code"] == "cloud_artifacts_present"
    assert r.json()["detail"]["owned"] == 1 and r.json()["detail"]["shared"] == 0 and r.json()["detail"]["total"] == 1
    assert count(linked, "cloud_link") == 1 and count(linked, "cloud_pointers") == 1
    assert cloud.seen("DELETE", "/me/server") == []  # nothing was revoked
    # bring it home: unlink is allowed
    assert bring(linked, aid).status_code == 200
    cloud.on("DELETE", "/me/server", status=204)
    r = linked.delete("/api/cloud/link", headers=linked.session)
    assert r.status_code == 200 and r.json()["cloud_revoked"] is True
    assert cloud.seen("DELETE", "/me/servers/srv1") == []  # the new route was enough


def test_unlink_with_force_goes_ahead_and_the_backup_survives(linked, cloud):
    aid = moved(linked, cloud)
    cloud.on("DELETE", "/me/server", status=204)
    r = linked.delete("/api/cloud/link", params={"force": "true"}, headers=linked.session)
    assert r.status_code == 200 and r.json()["unlinked"] is True and r.json()["cloud_revoked"] is True
    assert count(linked, "cloud_pointers") == 0 and count(linked, "artifact_cloud_backups", "artifact_id = ?", (aid,)) == 1


@pytest.mark.parametrize("status", [404, 405])
def test_unlink_falls_back_when_the_cloud_has_no_self_revoke(linked, cloud, status):
    cloud.on("DELETE", "/me/server", status=status, json={"code": "not_found", "message": "no"})
    cloud.on("DELETE", "/me/servers/srv1", status=403, json={"code": "forbidden_scope", "message": "session only"})
    r = linked.delete("/api/cloud/link", headers=linked.session)
    assert r.status_code == 200 and r.json()["unlinked"] is True and r.json()["cloud_revoked"] is False
    assert "403" in r.json()["message"] and len(cloud.seen("DELETE", "/me/servers/srv1")) == 1


def test_unlink_reports_a_cloud_that_is_down(linked, cloud):
    cloud.down = True
    r = linked.delete("/api/cloud/link", headers=linked.session)
    assert r.status_code == 200 and r.json()["cloud_revoked"] is False and "could not be asked" in r.json()["message"]


# ── paired devices only ──────────────────────────────────────────────────────
GID = "a" * 32
ROUTES = [
    ("POST", f"/api/artifacts/{GID}/move-to-cloud", None),
    ("POST", f"/api/artifacts/{GID}/bring-home", None),
    ("POST", f"/api/artifacts/{GID}/undo-bring-home", None),
    ("GET", f"/api/cloud/gacabits/{GID}/members", None),
    ("PATCH", f"/api/cloud/gacabits/{GID}/members/acct_x", {"role": "viewer"}),
    ("DELETE", f"/api/cloud/gacabits/{GID}/members/acct_x", None),
    ("POST", f"/api/cloud/gacabits/{GID}/leave", None),
    ("GET", f"/api/cloud/gacabits/{GID}/invites", None),
    ("POST", f"/api/cloud/gacabits/{GID}/invites", {"role": "viewer"}),
    ("DELETE", f"/api/cloud/gacabits/{GID}/invites/inv1", None),
    ("GET", f"/api/cloud/gacabits/{GID}/validations", None),
    ("POST", f"/api/cloud/gacabits/{GID}/validations", {"subject": "share"}),
    ("POST", f"/api/cloud/gacabits/{GID}/publish", {"confirmed_preview": True, "preview_hash": "h"}),
    ("PATCH", f"/api/cloud/templates/{GID}", {"visibility": "listed"}),
    ("DELETE", f"/api/cloud/templates/{GID}", None),
    ("POST", f"/api/cloud/templates/{GID}/recheck", None),
]


@pytest.mark.parametrize("scopes", [("artifacts",), ("artifacts", "cloud"), ("cloud",), ("alerts", "briefings", "artifacts", "heartbeat", "cloud")])
@pytest.mark.parametrize("method,path,body", ROUTES, ids=[f"{m} {p.split(GID)[-1] or p}" for m, p, _ in ROUTES])
def test_an_api_token_can_call_none_of_them_even_with_the_cloud_scope(linked, cloud, scopes, method, path, body):
    token = mint(linked, scopes=scopes)
    r = linked.request(method, path, headers=token, json=body)
    assert r.status_code in (403, 404), (r.status_code, r.text)
    assert r.status_code == 403
    assert cloud.requests == []  # and nothing reached the cloud


@pytest.mark.parametrize("method,path,body", ROUTES, ids=[f"{m} {p.split(GID)[-1] or p}" for m, p, _ in ROUTES])
def test_no_credential_is_401_not_a_cloud_call(linked, cloud, method, path, body):
    r = linked.request(method, path, json=body)
    assert r.status_code == 401 and cloud.requests == []


@pytest.mark.parametrize("host", [
    "s3" + "-a" * 2500 + "!.amazonaws.com", "s3" + "-a" * 28 + "!.amazonaws.com", "s3" + ".a-" * 1000 + ".amazonaws.com",
    "a" * 5000 + ".s3.amazonaws.com", "s3." + "a." * 2000 + "amazonaws.com", "-" * 5000 + ".amazonaws.com",
    ("a-" * 30 + "a.") * 100 + "s3.amazonaws.com",
])
def test_the_host_check_cannot_be_made_to_burn_cpu(host):
    """A hostile address must be refused in microseconds: the check runs on the event loop."""
    import time

    url = f"https://{host}/k"
    started = time.perf_counter()
    for _ in range(5):
        with pytest.raises(Exception):
            cloud_transfer.check_url(url)
        assert not cloud_transfer.s3_host(host)
    assert (time.perf_counter() - started) / 10 < 0.005


def test_no_regex_in_the_cloud_modules_has_a_nested_quantifier():
    """The class of bug behind the host check: `(a+)+`, `(?:x[...]*)*` and friends."""
    import pathlib
    import re

    bad = re.compile(r"\((?:\?:)?[^()]*[+*][^()]*\)[+*{]")
    root = pathlib.Path(cloud_transfer.__file__).parent
    for path in sorted(root.glob("cloud_*.py")):
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if "re.compile" in line or "re.fullmatch" in line or "re.match" in line:
                assert not bad.search(line), f"{path.name}:{n}: {line.strip()}"
