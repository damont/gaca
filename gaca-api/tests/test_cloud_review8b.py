"""Fixes from the independent review of Part 8b (PR #65): strict verification of everything the cloud stores,
what a move does not carry (preview + acknowledgement), backup retention, event-loop responsiveness, the
list/bring-home race, real in-flight locking, caps pinned by bytes read, per-user backups, member
acknowledgement, unlink counting unconfirmed bring-homes, transfer wall-clock timeouts, and one error shape."""
import asyncio
import copy
import gzip
import json
import random
import sqlite3
import threading
import time
import tracemalloc
from datetime import timedelta

import httpx
import pytest

from gaca_api.config import settings
from gaca_api.models.base import utcnow
from gaca_api.services import artifact_locks, cloud_bundle as cb, cloud_move, cloud_transfer
from tests.cloud_backend import FakeBackend
from tests.cloud_support import SERVER_TOKEN, gacabit, http, link  # noqa: F401  (fixtures)
from tests.test_cloud_move_http import (  # noqa: F401  (fixtures and helpers)
    bring, cloud, count, hdr, linked, make_page, move, moved, snapshot, undo,
)


def ids(*names):
    return list(names)


# ═════════════════════ 3. verification is type-strict and covers everything ═════════════════════
def ms(value):
    """What the cloud stores of a time: the same instant, cut to the millisecond."""
    from datetime import datetime, timezone

    d = datetime.fromisoformat(value).astimezone(timezone.utc)
    return d.replace(microsecond=d.microsecond // 1000 * 1000).isoformat()


def cloud_view(bundle):
    """What a faithful cloud would answer for this bundle, in the shapes cloud_move._verify reads."""
    meta = bundle["meta"]
    older = bundle.get("html_versions") or []
    g = {"role": "owner", "status": "active", "title": meta["title"], "description": meta.get("description"),
         "data_notes": meta.get("data_notes"), "archived": bool(meta.get("archived")), "version": bundle["html_version"],
         "schemas": copy.deepcopy(bundle["schemas"])}
    data = {"rev": 0, "state": copy.deepcopy(bundle["state"]), "state_version": bundle["state_version"], "collections": {
        name: [{"id": i, "data": copy.deepcopy(d["data"]), "created_at": ms(d["created_at"]), "updated_at": ms(d["updated_at"])}
               for i, d in docs.items()] for name, docs in bundle["collections"].items()}}
    versions = {"current": bundle["html_version"], "versions": [
        {"version": v["version"], "created_at": ms(v["created_at"]), "note": v["note"]} for v in older] + [
        {"version": bundle["html_version"], "created_at": "2026-01-01T00:00:00Z", "note": "imported"}]}
    return {"bundle": bundle, "g": g, "data": data, "html": bundle["html"], "versions": versions,
            "old_html": {v["version"]: v["html"] for v in older}, "events": {"events": [
                {"name": e["name"], "payload": copy.deepcopy(e.get("payload") or {}), "created_at": ms(e["created_at"])}
                for e in bundle["events"]]}}


def run_compare(view):
    return cb.compare_with_cloud(view["bundle"], view["g"], view["data"], view["html"], view["versions"],
                                 view["old_html"], view["events"])


@pytest.fixture
def bundle(linked):
    aid = make_page(linked, versions=4, events=3, handled=1)
    snap = snapshot(linked, aid)
    from gaca_api.db import cloud_move as move_db  # noqa: F401

    snap["artifact"]["doc"].update(description="weekly", data_notes="notes")
    b = cb.build_bundle(snap, utcnow())
    b["state"] = {"n": 1, "f": 1.5, "t": True, "nested": {"list": [1, 2, {"k": "é"}]}}
    return b


def test_an_identical_copy_compares_clean(bundle):
    assert run_compare(cloud_view(bundle)) == []


MUTATIONS = {
    # name: (mutator on the view, what must be reported)
    "title": (lambda v: v["g"].update(title="Other"), "title"),
    "description": (lambda v: v["g"].update(description="other"), "description"),
    "description_dropped": (lambda v: v["g"].update(description=None), "description"),
    "data_notes": (lambda v: v["g"].update(data_notes="other"), "data notes"),
    "archived": (lambda v: v["g"].update(archived=True), "archived"),
    "page_version": (lambda v: v["g"].update(version=99), "page version"),
    "page_version_as_bool": (lambda v: v["g"].update(version=True), "page version"),
    "schemas": (lambda v: v["g"].update(schemas={"items": {"fields": {"x": {"type": "string"}}}}), "collection schemas"),
    "owner": (lambda v: v["g"].update(role="viewer"), "owner"),
    "status": (lambda v: v["g"].update(status="brought_home"), "status brought_home"),
    "state_value": (lambda v: v["data"]["state"].update(n=2), "state"),
    "state_key_missing": (lambda v: v["data"]["state"].pop("n"), "state"),
    "state_bool_for_int": (lambda v: v["data"]["state"].update(n=True), "state"),
    "state_int_for_float": (lambda v: v["data"]["state"].update(f=1), "state"),
    "state_float_for_int": (lambda v: v["data"]["state"].update(n=1.0), "state"),
    "state_int_for_bool": (lambda v: v["data"]["state"].update(t=1), "state"),
    "state_nfc_nfd": (lambda v: v["data"]["state"]["nested"]["list"][2].update(k="é"), "state"),
    "state_big_int_rounded": (lambda v: v["data"]["state"].update(n=2 ** 53 + 1), "state"),
    "state_version": (lambda v: v["data"].update(state_version=77), "state version"),
    "doc_missing": (lambda v: v["data"]["collections"]["items"].pop(), "document counts"),
    "doc_extra": (lambda v: v["data"]["collections"]["items"].append(
        {"id": "zz", "data": {}, "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z"}), "document counts"),
    "collection_missing": (lambda v: v["data"]["collections"].pop("log"), "document counts"),
    "doc_data": (lambda v: v["data"]["collections"]["items"][0]["data"].update(text="x"), "documents"),
    "doc_bool_for_int": (lambda v: v["data"]["collections"]["log"][0]["data"].update(at=True), "documents"),
    "doc_float_for_int": (lambda v: v["data"]["collections"]["log"][0]["data"].update(at=1.0), "documents"),
    "doc_created_at": (lambda v: v["data"]["collections"]["items"][0].update(created_at="2020-01-01T00:00:00Z"), "document times"),
    "doc_updated_at": (lambda v: v["data"]["collections"]["items"][0].update(updated_at="2020-01-01T00:00:00Z"), "document times"),
    "html": (lambda v: v.update(html=v["html"] + " "), "page"),
    "old_version_html": (lambda v: v["old_html"].update({min(v["old_html"]): "<p>drift</p>"}), "page versions"),
    "old_version_missing": (lambda v: v["versions"]["versions"].pop(0), "page versions"),
    "old_version_extra": (lambda v: v["versions"]["versions"].append({"version": 500, "note": None, "created_at": "x"}), "page versions"),
    "old_version_note": (lambda v: v["versions"]["versions"][0].update(note="changed"), "page version times"),
    "old_version_time": (lambda v: v["versions"]["versions"][0].update(created_at="2020-01-01T00:00:00Z"), "page version times"),
    "event_missing": (lambda v: v["events"]["events"].pop(), "events"),
    "event_extra": (lambda v: v["events"]["events"].append({"name": "x", "payload": {}, "created_at": "2026-01-01T00:00:00Z"}), "events"),
    "event_name": (lambda v: v["events"]["events"][0].update(name="renamed"), "events"),
    "event_payload": (lambda v: v["events"]["events"][0].update(payload={"n": 9}), "events"),
    "event_payload_bool": (lambda v: v["events"]["events"][0].update(payload={"n": True}), "events"),
    "event_time": (lambda v: v["events"]["events"][0].update(created_at="2020-01-01T00:00:00Z"), "event times"),
    "event_order": (lambda v: v["events"]["events"].reverse(), "events"),
}


@pytest.mark.parametrize("name", list(MUTATIONS))
def test_each_stored_part_is_compared_and_a_difference_is_named(bundle, name):
    view = cloud_view(bundle)
    mutate, expected = MUTATIONS[name]
    if name in ("doc_data", "doc_bool_for_int", "doc_float_for_int", "doc_created_at", "doc_updated_at", "doc_missing",
                "doc_extra", "collection_missing") and not bundle["collections"]:
        pytest.skip("no documents")
    mutate(view)
    assert expected in run_compare(view), (name, run_compare(view))


def test_times_are_compared_to_the_millisecond_the_cloud_keeps(bundle):
    view = cloud_view(bundle)
    sent = view["data"]["collections"]["items"][0]["created_at"]
    view["bundle"]["collections"]["items"][view["data"]["collections"]["items"][0]["id"]]["created_at"] = "2026-10-02T10:00:00.123456+00:00"
    view["data"]["collections"]["items"][0]["created_at"] = "2026-10-02T10:00:00.123Z"  # cut to the millisecond: fine
    assert "document times" not in run_compare(view)
    view["data"]["collections"]["items"][0]["created_at"] = "2026-10-02T10:00:00.124Z"
    assert "document times" in run_compare(view)
    del sent


# the same, end to end: the cloud's copy drifts after the import and the page must NOT be switched
TAMPERS = {
    "state_bool_for_int": lambda g: g["state"].update(n=True) if "n" in g["state"] else None,
    "state_float_for_int": lambda g: g["state"].update(n=1.0) if "n" in g["state"] else None,
    "doc_bool": lambda g: g["docs"][("log", "l1")]["data"].update(at=True),
    "doc_float": lambda g: g["docs"][("log", "l1")]["data"].update(at=1.0),
    "doc_time": lambda g: g["docs"][("items", "i1")].update(created_at="2020-01-01T00:00:00+00:00"),
    "doc_data": lambda g: g["docs"][("items", "i1")]["data"].update(text="drift"),
    "html": lambda g: g.update(html=g["html"] + "!"),
    "page_version": lambda g: g.update(version=g["version"] + 1),
    "old_version_html": lambda g: g["versions"][0].update(html="<p>drift</p>"),
    "event": lambda g: g["events"][0].update(payload={"n": 99}),
    "event_dropped": lambda g: g["events"].pop(),
    "event_time": lambda g: g["events"][0].update(created_at="2020-01-01T00:00:00+00:00"),
    "description": lambda g: g.update(description="drift"),
    "data_notes": lambda g: g.update(data_notes="drift"),
    "title": lambda g: g.update(title="drift"),
    "archived": lambda g: g.update(archived=True),
    "schemas": lambda g: g.update(schemas={}),
}


@pytest.mark.parametrize("name", list(TAMPERS))
def test_a_cloud_copy_that_drifts_is_never_adopted(linked, cloud, name):
    aid = make_page(linked)
    before = snapshot(linked, aid)
    cloud.tamper = TAMPERS[name]
    r = move(linked, aid)
    assert r.status_code == 502 and r.json()["code"] == "verify_failed", r.text
    assert r.json()["info"]["mismatches"] and snapshot(linked, aid) == before
    assert count(linked, "cloud_pointers") == 0 and count(linked, "artifact_cloud_backups") == 0


def test_numbers_the_cloud_cannot_store_are_refused_before_anything_is_sent(linked, cloud):
    aid = make_page(linked, versions=1, events=0, handled=0, state={"big": 2 ** 70, "ok": 2 ** 62, "neg": -(2 ** 63)})
    before = snapshot(linked, aid)
    r = move(linked, aid)
    assert r.status_code == 422 and r.json()["code"] == "unsupported_number", r.text
    assert r.json()["info"]["paths"] == ["/state/big"] and r.json()["phase"] == "preflight"
    assert cloud.requests == [] and snapshot(linked, aid) == before


def test_nan_and_infinity_are_named_by_path(linked, cloud):
    aid = make_page(linked, versions=1, events=0, handled=0)
    con = sqlite3.connect(linked.db)
    con.execute("UPDATE artifacts SET state = ? WHERE id = ?", ('{"a": [1, NaN], "b": Infinity}', aid))
    con.commit()
    con.close()
    r = move(linked, aid)
    assert r.status_code == 422 and r.json()["code"] == "unsupported_number"
    assert r.json()["info"]["paths"] == ["/state/a/1", "/state/b"] and cloud.requests == []


def test_the_edge_of_int64_is_allowed(linked, cloud):
    aid = make_page(linked, versions=1, events=0, handled=0, state={"hi": 2 ** 63 - 1, "lo": -(2 ** 63), "f": 2 ** 53 + 1})
    assert move(linked, aid).status_code == 200
    assert cloud.gacabits[aid]["state"]["f"] == 2 ** 53 + 1


def test_the_cloud_refusing_a_number_is_reported_at_the_importing_phase(linked, cloud, monkeypatch):
    """If our own preflight missed one, the cloud's 422 is passed on cleanly and nothing changes."""
    aid = make_page(linked, versions=1, events=0, handled=0)
    monkeypatch.setattr(cb, "bad_numbers", lambda bundle, limit=10: [])
    con = sqlite3.connect(linked.db)
    con.execute("UPDATE artifacts SET state = ? WHERE id = ?", (json.dumps({"big": 2 ** 70}), aid))
    con.commit()
    con.close()
    before = snapshot(linked, aid)
    r = move(linked, aid)
    assert r.status_code == 422 and r.json()["code"] == "bundle_invalid" and r.json()["phase"] == "importing"
    assert snapshot(linked, aid) == before


# ═════════════════════ 4. what a move does not carry ═════════════════════
def test_the_preview_says_what_is_not_carried_and_sends_nothing(linked, cloud):
    aid = make_page(linked, versions=9, events=4, handled=2)
    con = sqlite3.connect(linked.db)
    con.execute("UPDATE artifact_docs SET version = 3 WHERE doc_id = 'i1'")
    doc = json.loads(con.execute("SELECT doc FROM artifacts WHERE id = ?", (aid,)).fetchone()[0])
    doc.update(session_id="s1", alert_id="a1", last_opened_at="2026-11-01T10:00:00Z")
    con.execute("UPDATE artifacts SET doc = ? WHERE id = ?", (json.dumps(doc), aid))
    con.commit()
    con.close()
    r = linked.get(f"/api/artifacts/{aid}/move-preview", headers=linked.session)
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["can_move"] is True and p["requires_acknowledgement"] is True and p["member_impact"] == "none"
    assert p["not_carried"] == {
        "versions_dropped": 4, "handled_events_dropped": 2, "crew": "mina",
        "chat_alert_links": {"session_id": "s1", "alert_id": "a1"}, "doc_versions_reset": 1, "last_opened": True,
        "anything": True}
    assert p["size"]["limit_bytes"] == 12 * 1024 * 1024 and p["size"]["bytes"] > 0 and p["size"]["documents"] == 4
    assert p["size"]["unhandled_events"] == 2 and p["backup_days"] == 30
    assert cloud.requests == [] and snapshot(linked, aid) is not None


def test_the_preview_of_a_page_that_does_not_fit_lists_why(linked, cloud):
    aid = make_page(linked, versions=1, events=0, handled=0)
    blob = "x" * 190_000
    for n in range(70):
        linked.post(f"/api/artifacts/{aid}/collections/big", headers=linked.session, json={"data": {"blob": blob}, "id": f"d{n}"})
    p = linked.get(f"/api/artifacts/{aid}/move-preview", headers=linked.session).json()
    assert p["can_move"] is False and p["blocked_because"] == "too_large_for_cloud"
    assert any(x["item"] == "the whole gacabit" for x in p["problems"]) and cloud.requests == []


def test_a_move_that_loses_something_needs_the_acknowledgement(linked, cloud):
    aid = make_page(linked)  # crew, handled events and more than 5 versions: all not carried
    before = snapshot(linked, aid)
    for body in (None, {}, {"acknowledge_not_carried": False}):
        r = linked.post(f"/api/artifacts/{aid}/move-to-cloud", headers=linked.session, json=body)
        assert r.status_code == 409 and r.json()["code"] == "confirmation_required", r.text
        assert r.json()["info"]["not_carried"]["crew"] == "mina" and isinstance(r.json()["detail"], str)
    assert cloud.requests == [] and snapshot(linked, aid) == before  # nothing sent, nothing changed
    assert linked.get(f"/api/artifacts/{aid}", headers=linked.session).json()["cloud_status"] is None
    done = move(linked, aid)  # acknowledged
    assert done.status_code == 200
    nc = done.json()["not_carried"]
    assert nc["crew"] == "mina" and nc["handled_events_dropped"] == 1 and nc["versions_dropped"] == 1 and nc["anything"] is True


def test_a_page_with_nothing_to_lose_moves_without_one(linked, cloud):
    aid = make_page(linked, versions=2, events=2, handled=0, crew=None)
    p = linked.get(f"/api/artifacts/{aid}/move-preview", headers=linked.session).json()
    assert p["requires_acknowledgement"] is False and p["not_carried"]["anything"] is False
    r = linked.post(f"/api/artifacts/{aid}/move-to-cloud", headers=linked.session)  # no body at all
    assert r.status_code == 200 and r.json()["not_carried"]["anything"] is False


def test_the_acknowledgement_is_not_a_way_around_the_size_check(linked, cloud):
    aid = make_page(linked, versions=1, events=0, handled=0)
    blob = "x" * 190_000
    for n in range(70):
        linked.post(f"/api/artifacts/{aid}/collections/big", headers=linked.session, json={"data": {"blob": blob}, "id": f"d{n}"})
    assert move(linked, aid).status_code == 413 and cloud.requests == []


def test_the_preview_is_paired_device_only(linked, cloud):
    from tests.cloud_support import mint

    for scopes in (("artifacts",), ("artifacts", "cloud")):
        r = linked.get(f"/api/artifacts/{'a' * 32}/move-preview", headers=mint(linked, scopes=scopes))
        assert r.status_code == 403
    assert linked.get(f"/api/artifacts/{'a' * 32}/move-preview").status_code == 401


# ── backup retention ─────────────────────────────────────────────────────────
def maint(linked, days):
    return linked.portal.call(cloud_move.maintenance, utcnow() + timedelta(days=days))


def test_a_backup_is_kept_until_30_days_and_the_cloud_copy_is_verified_alive(linked, cloud):
    aid = moved(linked, cloud)
    cloud.serve_list = False
    con = sqlite3.connect(linked.db)
    created, expires = con.execute("SELECT created_at, expires_at FROM artifact_cloud_backups").fetchone()
    con.close()
    from datetime import datetime

    assert datetime.fromisoformat(expires) - datetime.fromisoformat(created) == timedelta(days=30)
    assert maint(linked, 10)["backups_removed"] == 0  # not due
    # due (day 31) but it is the newest backup of a page that still has a pointer: kept for 90 days
    assert maint(linked, 31) == {"backups_removed": 0, "backups_kept": 1, "confirms_sent": 0}
    assert cloud.seen("GET", f"/gacabits/{aid}") == []  # (no need to ask: the rule alone keeps it)
    # day 91 and the cloud copy is alive and active: now it may go
    assert maint(linked, 91)["backups_removed"] == 1 and count(linked, "artifact_cloud_backups") == 0


@pytest.mark.parametrize("trouble", ["missing", "unreachable", "brought_home", "error"])
def test_a_backup_is_not_deleted_when_the_cloud_copy_cannot_be_confirmed(linked, cloud, caplog, trouble):
    aid = moved(linked, cloud)
    if trouble == "missing":
        cloud.gacabits.pop(aid)
    elif trouble == "unreachable":
        cloud.down = True
    elif trouble == "brought_home":
        cloud.gacabits[aid]["status"] = "brought_home"
    else:
        cloud.on("GET", f"/gacabits/{aid}", status=500, json={"code": "boom", "message": "x"})
        cloud.gacabits.pop(aid)
    caplog.set_level("WARNING")
    out = maint(linked, 120)
    assert out["backups_removed"] == 0 and out["backups_kept"] == 1 and count(linked, "artifact_cloud_backups") == 1
    assert "keeping the cloud backup" in caplog.text and aid in caplog.text
    # it is checked again the next day, not every hour
    cloud.requests.clear()
    linked.portal.call(cloud_move.maintenance, utcnow() + timedelta(days=120, hours=1))
    assert cloud.seen("GET", f"/gacabits/{aid}") == []
    cloud.down = False
    cloud.gacabits[aid] = cloud.gacabits.get(aid) or None
    if cloud.gacabits[aid] is None:
        cloud.gacabits.pop(aid)


def test_a_backup_goes_once_the_cloud_copy_is_seen_healthy_again(linked, cloud):
    aid = moved(linked, cloud)
    g = cloud.gacabits[aid]
    g["status"] = "brought_home"
    assert maint(linked, 100)["backups_removed"] == 0
    g["status"] = "active"
    out = linked.portal.call(cloud_move.maintenance, utcnow() + timedelta(days=102))
    assert out["backups_removed"] == 1


def test_a_page_that_is_local_again_no_longer_needs_its_backup(linked, cloud):
    aid = moved(linked, cloud)
    assert bring(linked, aid).status_code == 200
    assert maint(linked, 31)["backups_removed"] == 1


def test_a_backup_with_no_pointer_to_check_is_kept_until_the_one_year_ceiling(linked, cloud, caplog):
    aid = moved(linked, cloud)
    con = sqlite3.connect(linked.db)
    con.execute("DELETE FROM cloud_pointers")  # e.g. a forced unlink: nothing to verify the cloud copy with
    con.commit()
    con.close()
    caplog.set_level("WARNING")
    assert maint(linked, 200)["backups_removed"] == 0 and "no pointer" in caplog.text
    assert maint(linked, 366)["backups_removed"] == 1
    del aid


def test_an_older_backup_of_the_same_page_goes_while_the_newest_stays(linked, cloud):
    aid = moved(linked, cloud)
    assert bring(linked, aid).status_code == 200
    assert undo(linked, aid).status_code == 200  # back to the cloud: a second backup of the same page
    assert count(linked, "artifact_cloud_backups") == 2
    out = maint(linked, 40)
    # day 40: the OLDER backup (superseded, cloud copy verified active) goes; the newest is protected for 90 days
    assert out == {"backups_removed": 1, "backups_kept": 1, "confirms_sent": 0}
    out = maint(linked, 95)  # past 90 days with an active cloud copy: the newest may go too
    assert out["backups_removed"] == 1 and count(linked, "artifact_cloud_backups") == 0


def test_list_backups_shows_days_left(linked, cloud, tmp_path):
    import os
    import subprocess
    import sys

    moved(linked, cloud)
    r = subprocess.run([sys.executable, "-m", "gaca_api.cli", "cloud", "list-backups"],
                       env={**os.environ, "GACA_DB_PATH": linked.db}, capture_output=True, text=True, cwd=os.getcwd())
    assert r.returncode == 0 and ("29 day(s) left" in r.stdout or "30 day(s) left" in r.stdout), r.stdout
    con = sqlite3.connect(linked.db)
    con.execute("UPDATE artifact_cloud_backups SET expires_at = '2020-01-01T00:00:00+00:00'")
    con.commit()
    con.close()
    r = subprocess.run([sys.executable, "-m", "gaca_api.cli", "cloud", "list-backups"],
                       env={**os.environ, "GACA_DB_PATH": linked.db}, capture_output=True, text=True, cwd=os.getcwd())
    assert "kept until the cloud copy is verified healthy" in r.stdout


# ═════════════════════ 5. the event loop stays free ═════════════════════
def lag_during(linked, work, probe_path="/api/artifacts"):
    """Run `work()` in a thread while another thread asks for a list as fast as it can; return the slowest list."""
    slowest, stop, errors = [0.0], threading.Event(), []

    def probe():
        while not stop.is_set():
            started = time.perf_counter()
            try:
                linked.get(probe_path, headers=linked.session)
            except Exception as e:  # noqa: BLE001
                errors.append(e)
            slowest[0] = max(slowest[0], time.perf_counter() - started)
            time.sleep(0.005)

    t = threading.Thread(target=probe, daemon=True)
    t.start()
    time.sleep(0.1)
    try:
        out = work()
    finally:
        stop.set()
        t.join(5)
    assert not errors, errors
    return slowest[0], out


def big_page(linked, docs=3000):
    aid = make_page(linked, versions=2, events=0, handled=0, crew=None, collections={}, schemas={})
    pad = "lorem ipsum dolor sit amet " * 100
    for start in range(0, docs, 200):
        ops = [{"op": "set", "collection": "log", "id": f"d{n}", "data": {"n": n, "pad": pad, "f": n / 7}}
               for n in range(start, min(docs, start + 200))]
        assert linked.post(f"/api/artifacts/{aid}/batch", headers=linked.session, json={"ops": ops}).status_code == 200
    return aid


def test_other_requests_are_not_stalled_by_moving_a_big_page(linked, cloud):
    aid = big_page(linked)
    cloud.serve_list = True
    slowest, r = lag_during(linked, lambda: move(linked, aid))
    assert r.status_code == 200, r.text
    assert slowest < 0.25, f"a list waited {slowest:.2f}s while a big page moved"


def test_other_requests_are_not_stalled_by_bringing_a_big_page_home(linked, cloud):
    aid = big_page(linked)
    assert move(linked, aid).status_code == 200
    cloud.serve_list = True
    slowest, r = lag_during(linked, lambda: bring(linked, aid))
    assert r.status_code == 200, r.text
    assert slowest < 0.25, f"a list waited {slowest:.2f}s while a big page came home"


# ═════════════════════ 6. a pointer never coexists with a local page ═════════════════════
def test_an_id_is_never_both_a_local_page_and_a_pointer(linked, cloud):
    """Bring-homes (which delete a pointer and create the local page in one step) race the list's sync (which
    creates pointers from the cloud's list). The cloud keeps listing the page throughout: the worst case."""
    cloud.serve_list = True
    cloud.transfer_limit = 10 ** 6
    aid = make_page(linked, versions=1, events=0, handled=0, crew=None)
    assert move(linked, aid).status_code == 200
    stop, violations, lists = threading.Event(), [], [0]

    def poll_lists():
        from gaca_api.services import cloud_list

        while not stop.is_set():
            cloud_list.forget("")  # no-op key; the real cache reset is below
            for key in list(cloud_list._synced):  # noqa: SLF001
                cloud_list._synced.pop(key, None)
            linked.get("/api/artifacts", headers=linked.session)
            lists[0] += 1

    def watch_invariant():
        con = sqlite3.connect(linked.db, timeout=5)
        while not stop.is_set():
            both = con.execute("SELECT count(*) FROM cloud_pointers p JOIN artifacts a ON a.id = p.id").fetchone()[0]
            if both:
                violations.append(both)
            time.sleep(0.0005)
        con.close()

    threads = [threading.Thread(target=poll_lists, daemon=True), threading.Thread(target=watch_invariant, daemon=True)]
    for t in threads:
        t.start()
    rnd = random.Random(8)
    try:
        for i in range(50):
            r = bring(linked, aid)
            assert r.status_code == 200, (i, r.text)
            time.sleep(rnd.random() * 0.01)
            r = undo(linked, aid)  # the cloud copy is brought_home until undone: the way back is the undo
            assert r.status_code == 200, (i, r.text)
            time.sleep(rnd.random() * 0.01)
    finally:
        stop.set()
        for t in threads:
            t.join(10)
    assert violations == [] and lists[0] > 10
    con = sqlite3.connect(linked.db)
    assert con.execute("SELECT count(*) FROM cloud_pointers p JOIN artifacts a ON a.id = p.id").fetchone()[0] == 0
    con.close()


def test_a_pointer_made_while_a_list_was_in_flight_is_not_pruned(linked, cloud):
    from gaca_api.services import cloud_list

    aid = make_page(linked, versions=1, events=0, handled=0, crew=None)
    other = gacabit(gid="c" * 32, role="owner")
    seen_by_list = {"gacabits": [other]}  # the listing was read before the move finished: it lacks the page

    def slow_list(request):
        move(linked, aid)  # the move completes in the middle of the list's fetch... but sequentially here
        return httpx.Response(200, json=seen_by_list)

    # Move first, then pretend the list we are about to apply is older than the pointer.
    assert move(linked, aid).status_code == 200
    ptr = linked.portal.call(cloud_list.db.get_pointer, linked_user(linked), aid)
    assert ptr is not None
    cloud.on("GET", "/gacabits", json=seen_by_list)
    cloud_list._synced.clear()  # noqa: SLF001
    cloud_list._failed.clear()  # noqa: SLF001
    del slow_list
    # A list that began BEFORE the pointer existed must not delete it. Simulate by moving the pointer's last_seen
    # into the future of the list start:
    con = sqlite3.connect(linked.db)
    con.execute("UPDATE cloud_pointers SET last_seen = '2999-01-01T00:00:00+00:00' WHERE id = ?", (aid,))
    con.commit()
    con.close()
    linked.get("/api/artifacts", headers=linked.session)
    assert count(linked, "cloud_pointers", "id = ?", (aid,)) == 1
    # ... while one that really is gone from the cloud is pruned once it is older than the listing
    con = sqlite3.connect(linked.db)
    con.execute("UPDATE cloud_pointers SET last_seen = '2000-01-01T00:00:00+00:00' WHERE id = ?", (aid,))
    con.commit()
    con.close()
    cloud_list._synced.clear()  # noqa: SLF001
    linked.get("/api/artifacts", headers=linked.session)
    assert count(linked, "cloud_pointers", "id = ?", (aid,)) == 0


def linked_user(http):
    con = sqlite3.connect(http.db)
    try:
        return con.execute("SELECT user_id FROM cloud_link").fetchone()[0]
    finally:
        con.close()


# ═════════════════════ 7. tests that were vacuous ═════════════════════
def test_a_write_during_a_REAL_move_is_refused_with_423(linked, cloud):
    """No hand-set lock: a slow S3 holds the real move in its upload phase while another request writes."""
    aid = make_page(linked, versions=1, events=0, handled=0, crew=None)
    inner = httpx.MockTransport(cloud)

    async def slow_s3(request):
        if request.url.host.endswith("amazonaws.com") and request.method == "PUT":
            await asyncio.sleep(1.0)
        return await inner.handle_async_request(request)

    cloud_transfer.transport_override = httpx.MockTransport(slow_s3)
    result = {}
    t = threading.Thread(target=lambda: result.update(r=move(linked, aid)))
    t.start()
    deadline = time.time() + 5
    while aid not in artifact_locks.MOVING and time.time() < deadline:
        time.sleep(0.01)
    assert aid in artifact_locks.MOVING, "the move never started"
    try:
        w = linked.patch(f"/api/artifacts/{aid}/state", headers=linked.session, json={"patch": {"x": 1}})
        assert w.status_code == 423 and w.json()["code"] == "artifact_moving" and isinstance(w.json()["detail"], str)
        assert linked.post(f"/api/artifacts/{aid}/events", headers=linked.session, json={"name": "e"}).status_code == 423
        assert linked.get(f"/api/artifacts/{aid}/state", headers=linked.session).status_code == 200
        item = linked.get(f"/api/artifacts/{aid}", headers=linked.session).json()
        assert item["moving"] is True and item["cloud_status"] == "moving"
    finally:
        t.join(20)
    assert result["r"].status_code == 200  # and the refused write did not disturb the move
    assert cloud.gacabits[aid]["state"] == {"list": ["a", "b"], "n": 1}  # it never landed anywhere
    assert aid not in artifact_locks.MOVING and aid not in artifact_locks.BUSY


def test_the_gunzip_cap_holds_while_inflating(monkeypatch):
    """A tiny file that would inflate to 200 MB is stopped at the cap: memory stays near the cap, not 200 MB."""
    bomb = gzip.compress(b'{"a":"' + b"0" * (200 * 1024 * 1024) + b'"}', 9)
    assert len(bomb) < 400_000
    monkeypatch.setattr(settings, "cloud_max_bundle_bytes", 2 * 1024 * 1024)
    tracemalloc.start()
    with pytest.raises(cb.BundleInvalid, match="larger"):
        cb.unpack(bomb)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < 12 * 1024 * 1024, f"inflated {peak} bytes before stopping"


class CountingStream(httpx.AsyncByteStream):
    def __init__(self, total, chunk=64 * 1024):
        self.total, self.chunk, self.sent = total, chunk, 0

    async def __aiter__(self):
        while self.sent < self.total:
            n = min(self.chunk, self.total - self.sent)
            self.sent += n
            yield b"x" * n
            await asyncio.sleep(0)


def run_get(url, stream):
    cloud_transfer.transport_override = httpx.MockTransport(lambda r: httpx.Response(200, stream=stream))
    try:
        return asyncio.run(cloud_transfer.get(url))
    finally:
        cloud_transfer.transport_override = None


def test_the_download_stops_reading_at_the_cap(monkeypatch):
    monkeypatch.setattr(settings, "cloud_max_transfer_bytes", 1024 * 1024)
    stream = CountingStream(500 * 1024 * 1024)
    with pytest.raises(Exception) as e:
        run_get("https://bucket.s3.amazonaws.com/k", stream)
    assert getattr(e.value, "code", "") == "bundle_invalid"
    assert stream.sent <= 1024 * 1024 + 2 * 64 * 1024, f"read {stream.sent} bytes past a 1 MiB cap"
    assert stream.sent >= 1024 * 1024


def test_a_download_within_the_cap_is_returned_whole(monkeypatch):
    monkeypatch.setattr(settings, "cloud_max_transfer_bytes", 1024 * 1024)
    assert len(run_get("https://bucket.s3.amazonaws.com/k", CountingStream(1000 * 1024))) == 1000 * 1024


def test_a_backup_belongs_to_its_user(linked, cloud):
    from gaca_api.db import cloud_move as move_db

    aid = moved(linked, cloud)
    owner = linked_user(linked)
    call = linked.portal.call
    assert [b["artifact_id"] for b in call(move_db.list_backups, owner)] == [aid]
    assert call(move_db.list_backups, "someone-else") == []
    assert call(move_db.find_backup, aid, "someone-else") is None
    backup_id = call(move_db.list_backups, owner)[0]["id"]
    assert call(move_db.find_backup, backup_id, "someone-else") is None  # by its own id too
    assert call(move_db.find_backup, backup_id, owner)["artifact_id"] == aid
    from gaca_api.services.exceptions import NotFound

    with pytest.raises(NotFound):
        call(cloud_move.restore_backup, aid, "someone-else")
    assert snapshot(linked, aid) is None  # and nothing was restored
    assert call(cloud_move.restore_backup, aid, owner)["artifact_id"] == aid
    assert snapshot(linked, aid) is not None


def test_bring_home_never_uses_another_users_backup(linked, cloud):
    aid = moved(linked, cloud)
    con = sqlite3.connect(linked.db)
    con.execute("UPDATE artifact_cloud_backups SET user_id = 'someone-else'")
    con.commit()
    con.close()
    assert bring(linked, aid).status_code == 200
    assert snapshot(linked, aid)["artifact"]["doc"]["crew"] is None  # the other user's backup supplied nothing


def test_a_local_page_that_appears_during_a_bring_home_is_never_overwritten(linked, cloud, monkeypatch):
    aid = moved(linked, cloud)
    real = cb.unpack
    seen = {}

    def unpack_then_a_page_appears(data, cap=None):
        out = real(data, cap)
        con = sqlite3.connect(linked.db)
        con.execute("INSERT INTO artifacts (id, user_id, version, state, state_version, rev, updated_at, doc, html) "
                    "VALUES (?, 'u', 1, '{\"mine\": 1}', 1, 0, '2026-01-01T00:00:00+00:00', ?, '<p>mine</p>')",
                    (aid, json.dumps({"id": aid, "user_id": "u", "title": "Mine", "created_at": "2026-01-01T00:00:00Z", "schemas": {}})))
        con.commit()
        con.close()
        seen["inserted"] = True
        return out

    monkeypatch.setattr(cb, "unpack", unpack_then_a_page_appears)
    r = bring(linked, aid)
    assert seen["inserted"] and r.status_code == 409 and r.json()["code"] == "conflict"
    assert snapshot(linked, aid)["artifact"]["state"] == {"mine": 1}  # untouched
    assert cloud.gacabits[aid]["status"] == "active"  # the cloud copy was put back


def test_the_transaction_itself_refuses_when_a_local_page_exists(linked, cloud, monkeypatch):
    """Even if the check under the lock were skipped, the atomic restore refuses (FileExistsError -> 409)."""
    from gaca_api.db import artifacts as artifacts_db

    aid = moved(linked, cloud)
    real = cb.unpack

    def unpack_then_a_page_appears(data, cap=None):
        out = real(data, cap)
        con = sqlite3.connect(linked.db)
        con.execute("INSERT INTO artifacts (id, user_id, version, state, state_version, rev, updated_at, doc, html) "
                    "VALUES (?, 'u', 1, '{}', 1, 0, '2026-01-01T00:00:00+00:00', ?, '<p>mine</p>')",
                    (aid, json.dumps({"id": aid, "user_id": "u", "title": "Mine", "created_at": "2026-01-01T00:00:00Z", "schemas": {}})))
        con.commit()
        con.close()
        return out

    async def never_found(*a, **k):
        return None

    monkeypatch.setattr(cb, "unpack", unpack_then_a_page_appears)
    monkeypatch.setattr(artifacts_db, "find", never_found)
    r = bring(linked, aid)
    assert r.status_code == 409 and r.json()["code"] == "conflict"
    assert snapshot(linked, aid)["artifact"]["doc"]["title"] == "Mine"


# the fake cloud models the real one where the review found gaps
def test_the_fake_cloud_answers_402_on_uploads_and_import_like_the_real_one(linked, cloud):
    cloud.billing_required = True
    aid = make_page(linked)
    r = move(linked, aid)
    assert r.status_code == 402 and r.json()["code"] == "billing_required" and r.json()["phase"] == "uploading"
    assert snapshot(linked, aid) is not None


def test_the_fake_cloud_rate_limits_uploads_and_transfers_per_hour(linked, cloud):
    cloud.upload_limit = 1
    a, b = make_page(linked, crew=None, versions=1, events=0, handled=0), make_page(linked, crew=None, versions=1, events=0, handled=0, title="B")
    assert move(linked, a).status_code == 200
    r = move(linked, b)
    assert r.status_code == 429 and r.headers["retry-after"] == "1800" and r.json()["code"] == "rate_limited"
    assert snapshot(linked, b) is not None
    cloud.upload_limit, cloud.transfer_limit, cloud.transfer_hits = 20, 1, 1
    r = bring(linked, a)  # the bring-home counts against the transfer limit too
    assert r.status_code == 429 and r.json()["code"] == "rate_limited"
    assert count(linked, "cloud_pointers", "id = ?", (a,)) == 1  # pointer intact


def test_what_the_cloud_does_not_keep_does_not_come_back_from_it(linked, cloud):
    aid = make_page(linked, versions=9, events=4, handled=2, crew="mina")
    assert move(linked, aid).status_code == 200
    g = cloud.gacabits[aid]
    assert "crew" not in g and [e["name"] for e in g["events"]] == ["picked2", "picked3"]  # handled ones stayed behind
    assert [v["version"] for v in g["versions"]] == [5, 6, 7, 8, 9] and all(d for d in g["docs"].values())
    exported = cloud.export_bundle(g)
    assert "crew" not in exported["meta"] and len(exported["html_versions"]) == 4
    # ... and the backup is what still has them for a while
    con = sqlite3.connect(linked.db)
    con.execute("DELETE FROM artifact_cloud_backups")
    con.commit()
    con.close()
    assert bring(linked, aid).status_code == 200
    back = snapshot(linked, aid)
    assert back["artifact"]["doc"]["crew"] is None and len(back["versions"]) == 5 and all(d["version"] == 1 for d in back["docs"])
    assert [e["name"] for e in back["events"]] == ["picked2", "picked3"]


# ═════════════════════ 8. members ═════════════════════
def test_bringing_home_a_shared_page_needs_the_acknowledgement(linked, cloud):
    aid = moved(linked, cloud)
    cloud.gacabits[aid]["member_count"] = 3
    r = bring(linked, aid)
    assert r.status_code == 409 and r.json()["code"] == "members_will_lose_access", r.text
    assert r.json()["info"] == {"member_count": 3, "others": 2, "phase": "starting"} and "2 other member" in r.json()["message"]
    assert cloud.seen("POST", f"/gacabits/{aid}/bring-home") == [] and cloud.gacabits[aid]["status"] == "active"
    assert count(linked, "cloud_pointers", "id = ?", (aid,)) == 1
    r = linked.post(f"/api/artifacts/{aid}/bring-home", headers=linked.session, json={"acknowledge_members": True})
    assert r.status_code == 200 and r.json()["members_lost_access"] == 2 and snapshot(linked, aid) is not None


def test_an_unshared_page_needs_no_member_acknowledgement(linked, cloud):
    aid = moved(linked, cloud)
    r = bring(linked, aid)
    assert r.status_code == 200 and r.json()["members_lost_access"] == 0


# ═════════════════════ 9. unlink counts unconfirmed bring-homes and running transfers ═════════════════════
def test_unlink_is_blocked_by_an_unconfirmed_bring_home(linked, cloud):
    aid = moved(linked, cloud)
    cloud.faults["confirm"] = ["network"]
    assert bring(linked, aid).json()["confirmed"] is False  # home, but the cloud copy is locked and not on the purge clock
    assert count(linked, "cloud_pointers") == 0
    r = linked.delete("/api/cloud/link", headers=linked.session)
    assert r.status_code == 409 and r.json()["code"] == "cloud_artifacts_present"
    assert r.json()["info"]["unconfirmed_bring_homes"] == 1 and r.json()["info"]["total"] == 0
    assert count(linked, "cloud_link") == 1
    cloud.on("DELETE", "/me/server", status=204)
    assert bring(linked, aid).json()["confirmed"] is True  # told now
    assert linked.delete("/api/cloud/link", headers=linked.session).status_code == 200


def test_unlink_is_blocked_while_a_transfer_is_running(linked, cloud):
    artifact_locks.BUSY.add("f" * 32)
    try:
        r = linked.delete("/api/cloud/link", headers=linked.session)
        assert r.status_code == 409 and r.json()["info"]["in_progress"] == 1
        cloud.on("DELETE", "/me/server", status=204)
        assert linked.delete("/api/cloud/link", params={"force": "true"}, headers=linked.session).status_code == 200
    finally:
        artifact_locks.BUSY.discard("f" * 32)


def test_force_unlink_keeps_the_backups_and_an_unconfirmed_row(linked, cloud):
    aid = moved(linked, cloud)
    cloud.faults["confirm"] = ["network"]
    assert bring(linked, aid).json()["confirmed"] is False
    moved_again = make_page(linked, title="Second")
    assert move(linked, moved_again).status_code == 200
    cloud.on("DELETE", "/me/server", status=204)
    assert linked.delete("/api/cloud/link", params={"force": "true"}, headers=linked.session).status_code == 200
    assert count(linked, "cloud_pointers") == 0
    assert count(linked, "artifact_cloud_backups") == 2 and count(linked, "artifact_bring_homes", "confirmed = 0") == 1


# ═════════════════════ 10. wall-clock timeouts on transfers ═════════════════════
def test_a_slow_drip_download_hits_the_total_timeout(monkeypatch):
    monkeypatch.setattr(settings, "cloud_transfer_total_seconds", 0.4)

    class Drip(httpx.AsyncByteStream):
        sent = 0

        async def __aiter__(self):
            while True:
                Drip.sent += 1
                yield b"x"  # one byte per read: never trips a per-read timeout, never reaches the size cap
                await asyncio.sleep(0.05)

    started = time.perf_counter()
    with pytest.raises(Exception) as e:
        run_get("https://bucket.s3.amazonaws.com/k", Drip())
    assert getattr(e.value, "code", "") == "transfer_failed" and time.perf_counter() - started < 2
    assert Drip.sent < 40


def test_a_slow_upload_hits_the_total_timeout(linked, cloud, monkeypatch):
    monkeypatch.setattr(settings, "cloud_transfer_total_seconds", 0.3)
    aid = make_page(linked, crew=None, versions=1, events=0, handled=0)
    inner = httpx.MockTransport(cloud)

    async def stuck_s3(request):
        if request.method == "PUT":
            await asyncio.sleep(30)
        return await inner.handle_async_request(request)

    cloud_transfer.transport_override = httpx.MockTransport(stuck_s3)
    started = time.perf_counter()
    r = move(linked, aid)
    assert r.status_code == 502 and r.json()["code"] == "transfer_failed" and time.perf_counter() - started < 10
    assert snapshot(linked, aid) is not None and count(linked, "cloud_pointers") == 0


# ═════════════════════ 11. housekeeping respects the lock ═════════════════════
def test_housekeeping_does_not_confirm_while_a_transfer_of_that_page_runs(linked, cloud):
    aid = moved(linked, cloud)
    cloud.faults["confirm"] = ["network"]
    assert bring(linked, aid).json()["confirmed"] is False
    artifact_locks.BUSY.add(aid)
    try:
        out = linked.portal.call(cloud_move.maintenance)
        assert out["confirms_sent"] == 0 and cloud.gacabits[aid]["confirmed"] is False
    finally:
        artifact_locks.BUSY.discard(aid)
    assert linked.portal.call(cloud_move.maintenance)["confirms_sent"] == 1 and cloud.gacabits[aid]["confirmed"] is True


# ═════════════════════ 12. one error shape ═════════════════════
def assert_shape(r, status=None):
    body = r.json()
    assert isinstance(body["code"], str) and isinstance(body["message"], str), body
    assert isinstance(body["detail"], str) and body["detail"] == body["message"], body  # the apps decode a string
    if "info" in body:
        assert isinstance(body["info"], dict)
    if status:
        assert r.status_code == status
    return body


def test_every_new_error_has_a_string_detail_and_structured_keys_beside_it(linked, cloud):
    from tests.cloud_support import mint

    aid = make_page(linked)
    seen = []
    # 409 confirmation_required, 413, 422, 423, 409 not-cloud-homed, 404, 409 unlink guard, 403 read-only link
    seen.append(assert_shape(linked.post(f"/api/artifacts/{aid}/move-to-cloud", headers=linked.session), 409))
    artifact_locks.MOVING.add(aid)
    try:
        seen.append(assert_shape(linked.patch(f"/api/artifacts/{aid}", headers=linked.session, json={"title": "x"}), 423))
    finally:
        artifact_locks.MOVING.discard(aid)
    seen.append(assert_shape(bring(linked, aid), 409))
    cloud.faults["uploads"] = [(402, "billing_required", "set up billing")]
    seen.append(assert_shape(move(linked, aid), 402))
    cloud.faults["bring_home"] = ["corrupt"]
    other = moved(linked, cloud, title="Other")
    seen.append(assert_shape(bring(linked, other)))
    seen.append(assert_shape(linked.delete("/api/cloud/link", headers=linked.session), 409))
    cloud.on("POST", f"/gacabits/{'a' * 32}/publish", status=422, json={"code": "validation_blocked", "message": "no", "detail": {"findings": [1]}})
    r = linked.post(f"/api/cloud/gacabits/{'a' * 32}/publish", headers=linked.session, json={})
    seen.append(assert_shape(r, 422))
    assert r.json()["info"] == {"findings": [1]}
    assert_shape(linked.post(f"/api/cloud/gacabits/{'a' * 32}/invites", headers={**linked.session, "content-type": "application/json"}, content=b"[1]"), 422)
    # the structured keys the apps and the app's own UI read
    moves = [b for b in seen if "phase" in b]
    assert moves and all(b["local_data_changed"] in (False, True) for b in moves if "local_data_changed" in b)
    del mint


def test_the_failed_bring_home_wording_is_honest_about_the_cloud_copy(linked, cloud):
    aid = moved(linked, cloud)
    cloud.faults["bring_home"] = ["corrupt"]
    msg = bring(linked, aid).json()["message"]
    assert "put back as it was" in msg and "locked" not in msg
    cloud.faults["bring_home"] = ["corrupt"]
    cloud.faults["undo_closed"] = [1]
    msg = bring(linked, aid).json()["message"]
    assert "locked" in msg and "put back" not in msg
