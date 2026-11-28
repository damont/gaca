"""Moving a page WITH photos to the cloud and home again: the photos travel with their ids (the page's data holds
them), a failed or partial photo upload changes nothing here, the local photo rows stay as part of the backup and
are tidied once nothing needs them, and bringing home replaces them with the cloud's (which may have changed)."""
import io
import sqlite3

from PIL import Image

from gaca_api.services import cloud_move
from tests.cloud_support import http, link  # noqa: F401  (fixtures)
from tests.test_cloud_move_http import bring, cloud, count, linked, make_page, move, snapshot  # noqa: F401


def jpeg(color=(200, 30, 30)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (1200, 900), color).save(out, "JPEG")
    return out.getvalue()


def add_photo(http, aid, color=(200, 30, 30)) -> str:
    r = http.post(f"/api/artifacts/{aid}/photos", content=jpeg(color),
                  headers={**http.session, "Content-Type": "image/jpeg"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def photo_ids(http, aid) -> set[str]:
    con = sqlite3.connect(http.db)
    try:
        return {r[0] for r in con.execute("SELECT id FROM artifact_photos WHERE artifact_id = ?", (aid,))}
    finally:
        con.close()


def test_photos_go_to_the_cloud_with_their_ids_and_come_home(linked, cloud):
    aid = make_page(linked)
    a, b = add_photo(linked, aid), add_photo(linked, aid, (0, 0, 200))
    linked.patch(f"/api/artifacts/{aid}/state", headers=linked.session, json={"patch": {"cover": a, "back": b}})

    preview = linked.get(f"/api/artifacts/{aid}/move-preview", headers=linked.session).json()
    assert preview["can_move"] is True and preview["size"]["photos"] == 2

    r = move(linked, aid)
    assert r.status_code == 200, r.text
    sent = cloud.gacabits[aid]["photos"]
    assert set(sent) == {a, b} and all(p["meta"]["added_by"] == "app" for p in sent.values())
    assert Image.open(io.BytesIO(sent[a]["full"])).size == (1200, 900)
    assert photo_ids(linked, aid) == {a, b}  # kept here with the backup
    # the page now reads its photos from the cloud, through the forwarder
    got = linked.post(f"/api/artifacts/{aid}/call", headers=linked.session,
                      json={"method": "photo.get", "args": {"id": a}})
    assert got.status_code == 200 and got.json()["id"] == a

    # while in the cloud: one photo goes, another is added on the website
    del sent[b]
    sent["ph_00000000000000ff"] = {"meta": {"id": "ph_00000000000000ff", "content_type": "image/jpeg", "width": 10,
                                            "height": 10, "size": 3, "added_by": "web",
                                            "created_at": "2026-11-28T10:00:00.000Z"},
                                   "full": jpeg((0, 200, 0)), "thumb": jpeg((0, 200, 0))}
    r = bring(linked, aid)
    assert r.status_code == 200, r.text
    assert photo_ids(linked, aid) == {a, "ph_00000000000000ff"}  # exactly the cloud's
    full = linked.get(f"/api/artifacts/{aid}/photos/{a}", headers=linked.session)
    assert full.status_code == 200 and full.content == sent[a]["full"]
    listed = linked.get(f"/api/artifacts/{aid}/photos", headers=linked.session).json()["photos"]
    assert {p["id"]: p["added_by"] for p in listed}["ph_00000000000000ff"] == "web"


def test_a_failed_photo_upload_changes_nothing_here_and_a_retry_finishes(linked, cloud):
    aid = make_page(linked)
    pid = add_photo(linked, aid)
    before = snapshot(linked, aid)
    cloud.faults["photos"] = [(503, "unavailable", "try later")]
    r = move(linked, aid)
    assert r.status_code >= 500 and r.json()["info"]["phase"] == "photos", r.text
    assert snapshot(linked, aid) == before and photo_ids(linked, aid) == {pid}
    assert count(linked, "cloud_pointers", "id = ?", (aid,)) == 0

    r = move(linked, aid)  # the cloud copy from the first try is reused, the photo sent (again, harmlessly)
    assert r.status_code == 200, r.text
    assert r.json()["resumed"] is True and set(cloud.gacabits[aid]["photos"]) == {pid}


def test_a_photo_the_cloud_does_not_list_stops_the_move(linked, cloud, monkeypatch):
    aid = make_page(linked)
    add_photo(linked, aid)
    real = cloud._photos

    def forgetful(request, g, tail):
        resp = real(request, g, tail)
        if request.method == "POST":
            g["photos"].clear()  # accepted, then lost
        return resp
    monkeypatch.setattr(cloud, "_photos", forgetful)
    r = move(linked, aid)
    assert r.status_code == 502 and r.json()["code"] == "verify_failed", r.text
    assert count(linked, "cloud_pointers", "id = ?", (aid,)) == 0


def test_a_failed_photo_download_leaves_the_page_in_the_cloud(linked, cloud):
    aid = make_page(linked)
    add_photo(linked, aid)
    assert move(linked, aid).status_code == 200
    cloud.faults["photos"] = [(500, "server_error", "boom")]
    r = bring(linked, aid)
    assert r.status_code >= 500 and r.json()["info"]["phase"] == "photos", r.text
    assert count(linked, "cloud_pointers", "id = ?", (aid,)) == 1 and snapshot(linked, aid) is None
    assert cloud.gacabits[aid]["status"] == "active"  # the cloud's bring-home was undone


def test_local_photo_rows_go_once_no_page_or_backup_needs_them(linked, cloud):
    aid = make_page(linked)
    pid = add_photo(linked, aid)
    assert move(linked, aid).status_code == 200
    con = sqlite3.connect(linked.db)
    try:
        con.execute("DELETE FROM artifact_cloud_backups WHERE artifact_id = ?", (aid,))
        con.commit()
    finally:
        con.close()
    out = linked.portal.call(cloud_move.maintenance)
    assert out["photos_removed"] == 1 and photo_ids(linked, aid) == set()
    assert pid in cloud.gacabits[aid]["photos"]  # the cloud's copy is untouched
