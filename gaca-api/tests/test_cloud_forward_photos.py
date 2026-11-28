"""Photos on a cloud-homed gacabit, through the forwarding layer: uploads are re-encoded HERE (so they fit the
cloud's 4 MiB cap and carry no EXIF) and tagged app/agent by who called; reads, deletes and the page's photo.get /
photo.list go to the cloud's /photos routes; photo.add stays the app's."""
import base64
import io
import json

from PIL import Image

from tests.cloud_support import cloud, http, link, mint  # noqa: F401  (fixtures)
from tests.test_cloud_forward_http import A, hdr, linked  # noqa: F401  (fixture)

PID = "ph_00000000000000aa"
OUT = {"id": PID, "content_type": "image/jpeg", "width": 2048, "height": 683, "size": 1, "added_by": "app",
       "created_at": "2026-11-28T00:00:00Z"}


def jpeg(w=4000, h=1333) -> bytes:
    exif = Image.Exif()
    exif[0x8825] = {1: "N", 2: (40.0, 26.0, 46.0)}
    out = io.BytesIO()
    Image.new("RGB", (w, h), (10, 120, 200)).save(out, "JPEG", exif=exif)
    return out.getvalue()


def test_an_upload_is_shrunk_and_stripped_here_and_tagged_by_the_caller(linked, cloud):
    cloud.on("POST", "/gacabits/g1/photos", status=201, json=OUT)
    r = linked.post(A + "/photos", headers=hdr(linked, **{"Content-Type": "image/jpeg", "X-Gaca-Photo-Source": "agent"}),
                    content=jpeg())
    assert r.status_code == 201 and r.json()["id"] == PID
    sent = cloud.seen("POST", "/gacabits/g1/photos")[0]
    assert sent.headers["x-gaca-photo-source"] == "app"  # a paired phone, whatever the app's own header said
    img = Image.open(io.BytesIO(sent.content))
    assert max(img.size) == 2048 and not dict(img.getexif())

    hermes = mint(linked, scopes=("artifacts", "cloud"))
    linked.post(A + "/photos", headers={**hermes, "Content-Type": "image/jpeg"}, content=jpeg(800, 600))
    assert cloud.seen("POST", "/gacabits/g1/photos")[1].headers["x-gaca-photo-source"] == "agent"


def test_a_bad_upload_is_refused_locally(linked, cloud):
    r = linked.post(A + "/photos", headers=hdr(linked, **{"Content-Type": "image/jpeg"}), content=b"nope")
    assert r.status_code == 422 and cloud.seen("POST", "/gacabits/g1/photos") == []


def test_a_chat_photo_goes_up_as_bytes(linked, cloud, monkeypatch):
    import sqlite3
    from types import SimpleNamespace

    from gaca_api.db import attachments as attachments_db

    con = sqlite3.connect(linked.db)
    owner = con.execute("SELECT id FROM users").fetchone()[0]
    con.close()
    chat = {"att_mine": (SimpleNamespace(user_id=owner), jpeg(900, 600)),
            "att_theirs": (SimpleNamespace(user_id="someone-else"), jpeg(900, 600))}

    async def find(attachment_id):
        return chat.get(attachment_id)
    monkeypatch.setattr(attachments_db, "find", find)

    cloud.on("POST", "/gacabits/g1/photos", status=201, json=OUT)
    r = linked.post(A + "/photos/from-attachment", headers=hdr(linked), json={"attachment_id": "att_mine"})
    assert r.status_code == 201, r.text
    assert Image.open(io.BytesIO(cloud.seen("POST", "/gacabits/g1/photos")[0].content)).size == (900, 600)
    for other in ("att_theirs", "nope"):
        assert linked.post(A + "/photos/from-attachment", headers=hdr(linked),
                           json={"attachment_id": other}).status_code == 404
    assert len(cloud.seen("POST", "/gacabits/g1/photos")) == 1


def test_reads_and_deletes_are_forwarded(linked, cloud):
    cloud.on("GET", "/gacabits/g1/photos", json={"photos": [OUT]})
    cloud.on("GET", f"/gacabits/g1/photos/{PID}", content=b"\xff\xd8\xffJPEG", headers={"content-type": "image/jpeg"})
    cloud.on("DELETE", f"/gacabits/g1/photos/{PID}", status=204)
    assert linked.get(A + "/photos", headers=hdr(linked)).json()["photos"][0]["id"] == PID
    r = linked.get(A + f"/photos/{PID}?thumb=true", headers=hdr(linked))
    assert r.status_code == 200 and r.content == b"\xff\xd8\xffJPEG" and r.headers["x-content-type-options"] == "nosniff"
    assert cloud.seen("GET", f"/gacabits/g1/photos/{PID}")[0].url.params["thumb"] == "true"
    assert linked.delete(A + f"/photos/{PID}", headers=hdr(linked)).status_code == 204


def test_the_page_bridge_reads_photos_and_leaves_add_to_the_app(linked, cloud):
    small = io.BytesIO()
    Image.new("RGB", (480, 160)).save(small, "JPEG")
    cloud.on("GET", f"/gacabits/g1/photos/{PID}", content=small.getvalue(), headers={"content-type": "image/jpeg"})
    cloud.on("GET", "/gacabits/g1/photos", json={"photos": [OUT]})
    r = linked.post(A + "/call", headers=hdr(linked), json={"method": "photo.get", "args": {"id": PID, "thumb": True}})
    assert r.status_code == 200, r.text
    got = r.json()
    assert (got["id"], got["content_type"], got["width"], got["height"]) == (PID, "image/jpeg", 480, 160)
    assert base64.b64decode(got["data"]) == small.getvalue()
    listed = linked.post(A + "/call", headers=hdr(linked), json={"method": "photo.list", "args": {}})
    assert listed.json()["photos"][0]["id"] == PID
    bad = linked.post(A + "/call", headers=hdr(linked), json={"method": "photo.get", "args": {"id": "../x"}})
    assert bad.status_code == 422
    add = linked.post(A + "/call", headers=hdr(linked), json={"method": "photo.add", "args": {}})
    assert add.status_code == 400 and "Update the app" in add.text
