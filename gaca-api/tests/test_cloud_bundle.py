"""The bundle transformations (services/cloud_bundle.py) and the atomic local swap, without HTTP."""
import asyncio
import gzip
import json
import sqlite3
from datetime import datetime, timezone

import pytest

from gaca_api.config import settings
from gaca_api.db import client
from gaca_api.db import cloud_move as db
from gaca_api.models.cloud import CloudPointer
from gaca_api.services import cloud_bundle as cb

NOW = datetime(2026, 11, 12, 12, 0, 0, tzinfo=timezone.utc)
ID = "a" * 32


def snap(**over):
    art = {
        "id": ID, "user_id": "u1", "archived": False, "version": 7, "state": {"k": [1, 2]}, "state_version": 3, "rev": 9,
        "updated_at": "2026-11-01T10:00:00+00:00", "position": 2, "html": "<p>v7</p>",
        "doc": {"id": ID, "user_id": "u1", "title": "T", "description": "d", "crew": "mina", "session_id": "s1",
                "alert_id": "al1", "data_notes": "n", "schemas": {}, "created_at": "2026-10-01T10:00:00Z",
                "last_opened_at": None},
    }
    base = {
        "format": 1, "artifact": art,
        "versions": [{"version": n, "created_at": f"2026-10-0{n}T10:00:00+00:00", "note": f"n{n}", "html": f"<p>v{n}</p>"}
                     for n in range(1, 8)],
        "docs": [{"collection": "items", "doc_id": "a", "version": 1, "created_at": "2026-10-02T10:00:00.123456+00:00",
                  "updated_at": "2026-10-02T10:00:00.123456+00:00", "data": {"t": "a"}},
                 {"collection": "items", "doc_id": "b", "version": 4, "created_at": "2026-10-02T10:00:00.123456+00:00",
                  "updated_at": "2026-10-03T10:00:00+00:00", "data": {"t": "b"}}],
        "events": [{"seq": 5, "name": "x", "created_at": "2026-10-05T10:00:00+00:00", "handled": 1, "payload": {}},
                   {"seq": 6, "name": "y", "created_at": "2026-10-06T10:00:00+00:00", "handled": 0, "payload": {"a": 1}}],
    }
    return {**base, **over}


def test_a_bundle_has_exactly_what_the_cloud_keeps_and_validates():
    b = cb.build_bundle(snap(), NOW)
    assert cb.validate_bundle(b) == []
    assert b["kind"] == "full" and b["meta"]["origin_id"] == ID and b["html_version"] == 7
    assert [v["version"] for v in b["html_versions"]] == [3, 4, 5, 6]  # 4 older + the current page = the 5 newest
    assert [e["name"] for e in b["events"]] == ["y"]  # only the unhandled
    assert list(b["collections"]["items"]) == ["a", "b"]  # created_at, then id
    assert b["collections"]["items"]["b"]["created_at"] == "2026-10-02T10:00:00.123456+00:00"  # not rounded on the way out
    # nothing local-only leaks
    text = json.dumps(b)
    for local_only in ("u1", "s1", "al1", "position", "last_opened_at", '"rev"'):
        assert local_only not in text


def test_an_archived_flag_and_optional_fields_are_carried_or_left_out():
    s = snap()
    s["artifact"]["doc"].update(description=None, data_notes=None, crew=None)
    b = cb.build_bundle(s, NOW)
    assert "description" not in b["meta"] and "crew" not in b["meta"] and cb.validate_bundle(b) == []


def test_every_limit_is_listed_with_its_size(monkeypatch):
    s = snap()
    s["artifact"]["html"] = "x" * 1_000_001
    s["docs"].append({"collection": "big", "doc_id": "d", "version": 1, "created_at": "2026-10-02T10:00:00+00:00",
                      "updated_at": "2026-10-02T10:00:00+00:00", "data": {"s": "y" * 200_001}})
    s["events"] += [{"seq": 100 + i, "name": "e", "created_at": "2026-10-06T10:00:00+00:00", "handled": 0, "payload": {}}
                    for i in range(200)]
    found = {p["item"]: p for p in cb.too_big(s, cb.build_bundle(s, NOW))}
    assert found["page (html)"]["limit"] == 1_000_000
    assert found["document big/d"]["limit"] == 200_000
    assert found["unhandled events (mark some handled)"]["size"] == 201
    monkeypatch.setattr(settings, "cloud_max_bundle_bytes", 1000)
    assert "the whole gacabit" in {p["item"] for p in cb.too_big(snap(), cb.build_bundle(snap(), NOW))}
    assert cb.too_big(snap(), cb.build_bundle(snap(), NOW)) != []
    monkeypatch.undo()
    assert cb.too_big(snap(), cb.build_bundle(snap(), NOW)) == []


def test_pack_and_unpack_round_trip_and_refuse_nan():
    b = cb.build_bundle(snap(), NOW)
    assert cb.unpack(cb.pack(b)) == b
    s = snap()
    s["artifact"]["state"] = {"x": float("nan")}
    with pytest.raises(cb.BundleInvalid):
        cb.pack(cb.build_bundle(s, NOW))


def test_unpack_refuses_bombs_truncation_garbage_and_constants():
    with pytest.raises(cb.BundleInvalid, match="larger"):
        cb.unpack(gzip.compress(b'{"a":"' + b"0" * 5000 + b'"}'), cap=1000)
    good = cb.pack(cb.build_bundle(snap(), NOW))
    with pytest.raises(cb.BundleInvalid, match="cut short"):
        cb.unpack(good[:-10])
    for bad in (b"garbage", b"", b"\x1f\x8bnope", gzip.compress(b"[1,2]"), gzip.compress(b'{"a": NaN}'),
                gzip.compress(b'{"a": Infinity}'), gzip.compress(b"\xff\xfe")):
        with pytest.raises(cb.BundleInvalid):
            cb.unpack(bad)
    # an already-inflated JSON body (a client that undid Content-Encoding) obeys the same ceiling
    assert cb.unpack(b'{"a":1}') == {"a": 1}
    with pytest.raises(cb.BundleInvalid, match="larger"):
        cb.unpack(b'{"a":"' + b"0" * 5000 + b'"}', cap=1000)


def test_a_bundle_becomes_a_local_snapshot_with_the_clouds_data():
    b = cb.build_bundle(snap(), NOW)
    out = cb.bundle_to_snapshot(b, user_id="u1", artifact_id=ID, cloud_rev=4, position=2, updated_at=NOW)
    a = out["artifact"]
    assert (a["id"], a["version"], a["state"], a["state_version"], a["position"]) == (ID, 7, {"k": [1, 2]}, 3, 2)
    assert a["rev"] == 5  # the cloud's rev + 1
    assert [d["doc_id"] for d in out["docs"]] == ["a", "b"] and all(d["version"] == 1 for d in out["docs"])
    assert out["docs"][0]["created_at"] == "2026-10-02T10:00:00.123456+00:00"
    assert [v["version"] for v in out["versions"]] == [3, 4, 5, 6, 7]
    assert [(e["name"], e["handled"]) for e in out["events"]] == [("y", 0)]
    assert a["doc"]["title"] == "T" and a["doc"]["crew"] == "mina" and a["doc"]["session_id"] is None  # not in a bundle


def test_the_backup_only_fills_what_a_bundle_cannot_carry():
    b = cb.build_bundle(snap(), NOW)
    b["meta"]["title"] = "Changed in the cloud"
    b["state"] = {"cloud": 1}
    b["meta"].pop("crew")
    out = cb.bundle_to_snapshot(b, user_id="u1", artifact_id=ID, cloud_rev=0, position=None, updated_at=NOW, prior=snap())
    assert out["artifact"]["doc"]["title"] == "Changed in the cloud" and out["artifact"]["state"] == {"cloud": 1}
    assert out["artifact"]["doc"]["crew"] == "mina" and out["artifact"]["doc"]["session_id"] == "s1"
    assert out["artifact"]["rev"] == 10  # at least the old local rev + 1
    cur = out["versions"][-1]  # the current page is unchanged, so its original time and note come back
    assert (cur["created_at"], cur["note"]) == ("2026-10-07T10:00:00+00:00", "n7")
    # a changed page does not borrow the old row
    b["html"] = "<p>other</p>"
    cur = cb.bundle_to_snapshot(b, user_id="u1", artifact_id=ID, cloud_rev=0, position=None, updated_at=NOW,
                                prior=snap())["versions"][-1]
    assert cur["note"] == "from the cloud"


@pytest.mark.parametrize("mutate", [
    lambda b: b.update(kind="template"), lambda b: b.update(extra=1), lambda b: b.update(bundle_version=2),
    lambda b: b.pop("html"), lambda b: b["meta"].pop("title"), lambda b: b["meta"].update(title=""),
    lambda b: b.update(collections={"UPPER": {}}), lambda b: b.update(collections={"x": {"": {"data": {}, "created_at": "t"}}}),
    lambda b: b.update(collections={"x": {"a": {"data": {}}}}),
    lambda b: b.update(events=[{"name": "", "created_at": "2026-01-01T00:00:00Z"}]),
    lambda b: b.update(state=[1]), lambda b: b.update(html="x" * 1_000_001),
    lambda b: b.update(html_versions=[{"version": 0, "created_at": "t", "html": "x"}]),
    lambda b: b.update(schemas={"items": {"fields": {"a": {"type": "nope"}}}}),
])
def test_a_bundle_that_breaks_the_format_is_refused(mutate):
    b = cb.build_bundle(snap(), NOW)
    mutate(b)
    with pytest.raises(cb.BundleInvalid):
        cb.bundle_to_snapshot(b, user_id="u1", artifact_id=ID, cloud_rev=0, position=None, updated_at=NOW)


@pytest.mark.parametrize("mutate,why", [
    (lambda b: b["collections"].update(x={"a": {"data": {"s": "y" * 200_001}, "created_at": "2026-01-01T00:00:00Z"}}), "document"),
    (lambda b: b["collections"].update(x={"a": {"data": {}, "created_at": "yesterday"}}), "date"),
    (lambda b: b.update(state={"s": "y" * 1_000_001}), "state"),
    (lambda b: b.update(html="é" * 600_000), "page"),  # 1.2 MB in UTF-8: the limit is bytes, not characters
])
def test_local_limits_are_enforced_on_what_comes_home(mutate, why):
    b = cb.build_bundle(snap(), NOW)
    mutate(b)
    with pytest.raises(cb.BundleInvalid):
        cb.bundle_to_snapshot(b, user_id="u1", artifact_id=ID, cloud_rev=0, position=None, updated_at=NOW)


def test_the_fingerprint_sees_content_and_ignores_opening_and_position():
    a = snap()
    f = db.fingerprint(a)
    b = snap()
    b["artifact"]["position"] = 99
    b["artifact"]["doc"]["last_opened_at"] = "2026-11-11T00:00:00Z"
    b["events"][0]["seq"] = 12345
    assert db.fingerprint(b) == f
    for change in (lambda s: s["artifact"].update(rev=10), lambda s: s["docs"][0]["data"].update(t="z"),
                   lambda s: s["events"][1].update(handled=1), lambda s: s["artifact"]["state"].update(n=1),
                   lambda s: s["versions"].pop(), lambda s: s["artifact"].update(html="<p>x</p>")):
        c = snap()
        change(c)
        assert db.fingerprint(c) != f


# ── the atomic swap, straight on the database ────────────────────────────────
@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "t.db"))

    async def open_():
        await client.connect()

    asyncio.run(open_())
    yield str(tmp_path / "t.db")
    asyncio.run(client.close())


def run(coro):
    return asyncio.run(coro)


def pointer():
    return CloudPointer(id=ID, user_id="u1", cloud_id=ID, account_id="acct", role="owner", title="T", position=2,
                        last_seen=NOW, meta={"id": ID, "title": "T", "role": "owner"})


def test_the_snapshot_round_trips_through_the_database_exactly(database):
    async def go():
        s = snap()
        await db.restore_snapshot(s, keep_event_seq=True)
        again = await db.read_snapshot("u1", ID)
        assert again == s
        return again

    run(go())


def test_the_swap_to_a_pointer_is_all_or_nothing(database, monkeypatch):
    async def go():
        s = snap()
        await db.restore_snapshot(s, keep_event_seq=True)
        stored = await db.read_snapshot("u1", ID)
        monkeypatch.setattr(db, "_upsert_pointer", lambda raw, p: (_ for _ in ()).throw(RuntimeError("late failure")))
        with pytest.raises(RuntimeError):
            await db.convert_to_pointer(stored, pointer(), backup_id="b1", created_at=NOW, expires_at=NOW)
        assert await db.read_snapshot("u1", ID) == stored and await db.list_backups() == []
        monkeypatch.undo()
        await db.convert_to_pointer(stored, pointer(), backup_id="b1", created_at=NOW, expires_at=NOW)
        assert await db.read_snapshot("u1", ID) is None
        (backup,) = await db.list_backups()
        assert backup["id"] == "b1"
        found = await db.find_backup(ID)
        assert found["snapshot"] == stored
        # restoring over an existing local page is refused and changes nothing
        await db.restore_snapshot(found["snapshot"], keep_event_seq=True)
        with pytest.raises(FileExistsError):
            await db.restore_snapshot(found["snapshot"], keep_event_seq=True)

    run(go())
    con = sqlite3.connect(database)
    assert con.execute("SELECT count(*) FROM artifact_events WHERE artifact_id = ?", (ID,)).fetchone()[0] == 2
    con.close()


def test_an_uncommitted_write_by_someone_else_does_not_break_the_swap(database):
    """The server shares one connection: another request may have written without committing yet."""
    async def go():
        s = snap()
        await db.restore_snapshot(s, keep_event_seq=True)
        stored = await db.read_snapshot("u1", ID)
        conn = client.connection()
        await conn.execute("INSERT INTO server_meta (key, value) VALUES ('stray', '1')")  # no commit
        await db.convert_to_pointer(stored, pointer(), backup_id="b1", created_at=NOW, expires_at=NOW)
        assert await db.read_snapshot("u1", ID) is None and len(await db.list_backups()) == 1

    run(go())
