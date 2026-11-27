"""Photos on artifact pages: upload (re-encoded, no EXIF), read back over HTTP and through the page's bridge,
chat photos copied onto a page, quotas, isolation, cleanup, and the cloud move refusing pages with photos."""
import asyncio
import base64
import io
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from gaca_api.config import settings
from gaca_api.db import client as db_client
from gaca_api.main import app
from gaca_api.models.base import utcnow
from gaca_api.models.user import CrewMember, UserDoc
from gaca_api.services import artifact_photos, cloud_move
from gaca_api.services import auth as auth_service
from gaca_api.services.exceptions import CloudError, InvalidRequest
from gaca_api.utils.auth import get_current_user

_current = {"id": "u1"}


class StubBackend:
    async def start_run(self, *, session_id, history, user_message, images=()):
        return "run_stub"

    async def stream_events(self, run_id, *, session_id):
        if False:
            yield None


@pytest.fixture
def http(tmp_path, monkeypatch):
    settings.db_path = str(tmp_path / "t.db")
    monkeypatch.setattr(settings, "agent_engine", settings.agent_engine or "hermes")
    monkeypatch.setattr(auth_service, "backend_for_crew", lambda user, member: StubBackend())
    app.dependency_overrides[get_current_user] = lambda: UserDoc(
        id=_current["id"], email=f"{_current['id']}@example.com", display_name="T",
        agent_host="h", crew=[CrewMember(name="default", port=1, api_key="k")], default_crew="default",
    )
    _current["id"] = "u1"
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def jpeg(w=3000, h=1000, *, gps=True, orientation=None) -> bytes:
    image = Image.new("RGB", (w, h), (200, 30, 30))
    exif = Image.Exif()
    exif[0x010F] = "PhoneMaker"  # Make
    if orientation:
        exif[0x0112] = orientation
    if gps:
        exif[0x8825] = {1: "N", 2: (40.0, 26.0, 46.0), 3: "W", 4: (79.0, 58.0, 56.0)}
    out = io.BytesIO()
    image.save(out, "JPEG", exif=exif)
    return out.getvalue()


def png_with_alpha() -> bytes:
    out = io.BytesIO()
    Image.new("RGBA", (300, 200), (0, 128, 0, 100)).save(out, "PNG")
    return out.getvalue()


def page(http) -> str:
    r = http.post("/api/artifacts", json={"title": "Meals", "html": "<p>meals</p>"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def upload(http, aid, data, ctype="image/jpeg"):
    return http.post(f"/api/artifacts/{aid}/photos", content=data, headers={"Content-Type": ctype})


def test_upload_is_reencoded_without_exif_and_turned_upright(http):
    aid = page(http)
    r = upload(http, aid, jpeg(orientation=6))  # 3000x1000 lying on its side: really 1000x3000
    assert r.status_code == 201, r.text
    photo = r.json()
    assert photo["id"].startswith("ph_") and len(photo["id"]) == 19
    assert (photo["width"], photo["height"]) == (683, 2048)
    assert photo["content_type"] == "image/jpeg" and photo["added_by"] == "app"

    full = http.get(f"/api/artifacts/{aid}/photos/{photo['id']}")
    assert full.status_code == 200 and full.headers["content-type"] == "image/jpeg"
    assert full.headers["x-content-type-options"] == "nosniff"
    stored = Image.open(io.BytesIO(full.content))
    assert stored.size == (683, 2048)
    assert not dict(stored.getexif())  # no make, no GPS, no orientation
    thumb = Image.open(io.BytesIO(http.get(f"/api/artifacts/{aid}/photos/{photo['id']}?thumb=true").content))
    assert max(thumb.size) == 480

    listed = http.get(f"/api/artifacts/{aid}/photos").json()["photos"]
    assert [p["id"] for p in listed] == [photo["id"]]


def test_transparency_is_kept_as_webp(http):
    aid = page(http)
    photo = upload(http, aid, png_with_alpha(), "image/png").json()
    assert photo["content_type"] == "image/webp"
    img = Image.open(io.BytesIO(http.get(f"/api/artifacts/{aid}/photos/{photo['id']}").content))
    assert img.mode == "RGBA" and img.size == (300, 200)  # small photos are never enlarged


def test_bad_uploads_are_refused(http):
    aid = page(http)
    assert upload(http, aid, b"").status_code == 422
    assert upload(http, aid, b"not an image at all").status_code == 422
    assert upload(http, aid, b"\xff\xd8\xff\xe0" + b"\x00" * 64).status_code == 422  # JPEG magic, no image
    too_big = b"\xff\xd8\xff" + b"\x00" * (16 * 1024 * 1024)
    assert upload(http, aid, too_big).status_code == 413
    assert http.get(f"/api/artifacts/{aid}/photos").json()["photos"] == []


def test_a_full_size_phone_photo_is_allowed_past_the_normal_body_limit(http):
    import os
    aid = page(http)
    noise = Image.frombytes("RGB", (1800, 1800), os.urandom(1800 * 1800 * 3))
    out = io.BytesIO()
    noise.save(out, "JPEG", quality=100)
    assert len(out.getvalue()) > settings.max_body_bytes  # bigger than any other request may be
    assert upload(http, aid, out.getvalue()).status_code == 201
    # ...but only for photo uploads.
    r = http.post(f"/api/artifacts/{aid}/events", content=b"x" * (settings.max_body_bytes + 10),
                  headers={"Content-Type": "application/json"})
    assert r.status_code == 413


def test_the_page_reads_photos_through_the_bridge(http):
    aid = page(http)
    photo = upload(http, aid, jpeg()).json()
    r = http.post(f"/api/artifacts/{aid}/call", json={"method": "photo.get", "args": {"id": photo["id"], "thumb": True}})
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["content_type"] == "image/jpeg" and max(got["width"], got["height"]) == 480
    assert Image.open(io.BytesIO(base64.b64decode(got["data"]))).size == (got["width"], got["height"])

    listed = http.post(f"/api/artifacts/{aid}/call", json={"method": "photo.list", "args": {}}).json()
    assert [p["id"] for p in listed["photos"]] == [photo["id"]]
    bad = http.post(f"/api/artifacts/{aid}/call", json={"method": "photo.get", "args": {"id": "../../x"}})
    assert bad.status_code == 422
    missing = http.post(f"/api/artifacts/{aid}/call", json={"method": "photo.get", "args": {"id": "ph_" + "0" * 16}})
    assert missing.status_code == 404


def test_an_app_that_cannot_pick_photos_gets_a_clear_answer(http):
    aid = page(http)
    r = http.post(f"/api/artifacts/{aid}/call", json={"method": "photo.add", "args": {}})
    assert r.status_code == 400 and "Update the app" in r.text


def test_a_chat_photo_can_be_put_on_a_page(http):
    aid = page(http)
    sid = http.post("/api/sessions", json={}).json()["id"]
    att = http.post(f"/api/sessions/{sid}/attachments", content=jpeg(800, 600),
                    headers={"Content-Type": "image/jpeg"}).json()
    r = http.post(f"/api/artifacts/{aid}/photos/from-attachment", json={"attachment_id": att["id"]})
    assert r.status_code == 201, r.text
    assert (r.json()["width"], r.json()["height"]) == (800, 600)
    assert http.post(f"/api/artifacts/{aid}/photos/from-attachment",
                     json={"attachment_id": "nope"}).status_code == 404

    _current["id"] = "u2"  # someone else can't copy u1's chat photo onto their page
    other = page(http)
    assert http.post(f"/api/artifacts/{other}/photos/from-attachment",
                     json={"attachment_id": att["id"]}).status_code == 404


def test_photos_are_private_to_their_owner_and_page(http):
    aid = page(http)
    pid = upload(http, aid, jpeg()).json()["id"]
    second = page(http)
    assert http.get(f"/api/artifacts/{second}/photos/{pid}").status_code == 404  # wrong page
    _current["id"] = "u2"
    assert http.get(f"/api/artifacts/{aid}/photos/{pid}").status_code == 404
    assert upload(http, aid, jpeg()).status_code == 404
    assert http.delete(f"/api/artifacts/{aid}/photos/{pid}").status_code == 404


def test_delete_one_photo_and_deleting_the_page_removes_them_all(http):
    aid = page(http)
    a = upload(http, aid, jpeg()).json()["id"]
    b = upload(http, aid, jpeg()).json()["id"]
    assert http.delete(f"/api/artifacts/{aid}/photos/{a}").status_code == 204
    assert http.get(f"/api/artifacts/{aid}/photos/{a}").status_code == 404
    assert http.delete(f"/api/artifacts/{aid}").status_code == 204

    async def remaining():
        async with db_client.connection().execute("SELECT COUNT(*) AS n FROM artifact_photos WHERE id = ?", (b,)) as c:
            return (await c.fetchone())["n"]
    assert http.portal.call(remaining) == 0


def test_unused_photos_are_swept_after_the_grace_period(http):
    aid = page(http)
    used = upload(http, aid, jpeg()).json()["id"]
    in_state = upload(http, aid, jpeg()).json()["id"]
    unused = upload(http, aid, jpeg()).json()["id"]
    fresh_unused = upload(http, aid, jpeg()).json()["id"]
    http.post(f"/api/artifacts/{aid}/collections/meals", json={"id": "tacos", "data": {"title": "Tacos", "photo": used}})
    http.patch(f"/api/artifacts/{aid}/state", json={"patch": {"cover": in_state}})

    async def age(ids):
        old = (utcnow() - artifact_photos.UNUSED_GRACE - timedelta(hours=1)).isoformat()
        conn = db_client.connection()
        for pid in ids:
            await conn.execute("UPDATE artifact_photos SET created_at = ? WHERE id = ?", (old, pid))
        await conn.commit()
    http.portal.call(age, [used, in_state, unused])

    upload(http, aid, jpeg())  # every upload tidies its page
    left = {p["id"] for p in http.get(f"/api/artifacts/{aid}/photos").json()["photos"]}
    assert used in left and in_state in left and fresh_unused in left
    assert unused not in left


def test_quota_per_page(http, monkeypatch):
    monkeypatch.setattr(artifact_photos, "MAX_PHOTOS_PER_PAGE", 2)
    aid = page(http)
    assert upload(http, aid, jpeg()).status_code == 201
    assert upload(http, aid, jpeg()).status_code == 201
    r = upload(http, aid, jpeg())
    assert r.status_code == 422 and "remove some" in r.text


def test_a_page_with_photos_is_not_moved_to_the_cloud(http):
    aid = page(http)
    upload(http, aid, jpeg())

    async def preview():
        return await cloud_move.move_preview("u1", aid)
    out = http.portal.call(preview)
    assert out["can_move"] is False and out["blocked_because"] == "has_photos"

    class Link:
        scope = "manage"
        account_id = "acc"

    async def move():
        async def link(_user):
            return Link()
        cloud_move.cloud_link.require_link, saved = link, cloud_move.cloud_link.require_link
        try:
            await cloud_move.move_to_cloud("u1", aid, acknowledge_not_carried=True)
        finally:
            cloud_move.cloud_link.require_link = saved
    with pytest.raises(CloudError) as info:
        http.portal.call(move)
    assert info.value.code == "has_photos"


def test_process_refuses_decompression_bombs(monkeypatch):
    monkeypatch.setattr(artifact_photos, "MAX_PIXELS", 1000)
    with pytest.raises(InvalidRequest):
        artifact_photos.process(jpeg(100, 100))


def test_the_sdk_and_preview_know_photos(http):
    r = http.post("/api/artifacts/preview", json={"title": "t", "html": "<p>x</p>"})
    assert "photo.get" in r.text and "app.photos.add() only works straight from a tap" in r.text
