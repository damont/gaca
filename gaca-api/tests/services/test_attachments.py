"""Photo attachments: storing, claiming, and sending them through a turn."""
import asyncio
import base64
import zlib
import struct

import pytest

from gaca_api.config import settings
from gaca_api.db import client
from gaca_api.db import messages as messages_db
from gaca_api.models.session import SessionDoc
from gaca_api.db import sessions as sessions_db
from gaca_api.schemas.event import RunCompleted
from gaca_api.schemas.message import ImageInput, MessageCreate, MessageOut
from gaca_api.services import attachments as svc
from gaca_api.services import runs
from gaca_api.services.exceptions import InvalidRequest, NotFound, PayloadTooLarge


def _png() -> bytes:
    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + bytes((200, 10, 10)) * 8 for _ in range(8))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 8, 8, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 20


def _run(coro_fn, tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def main():
        await client.connect()
        try:
            await sessions_db.insert(SessionDoc(id="s1", user_id="u1"))
            await sessions_db.insert(SessionDoc(id="s2", user_id="u1"))
            await sessions_db.insert(SessionDoc(id="sx", user_id="u2"))
            return await coro_fn()
        finally:
            await client.close()

    return asyncio.run(main())


def test_sniff_trusts_the_bytes_not_the_claim():
    assert svc.sniff(_png()) == "image/png"
    assert svc.sniff(JPEG) == "image/jpeg"
    assert svc.sniff(b"GIF89a....") == "image/gif"
    assert svc.sniff(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert svc.sniff(b"<html>not an image</html>") is None


def test_store_rejects_bad_empty_oversized_and_foreign_sessions(tmp_path):
    async def go():
        with pytest.raises(InvalidRequest):
            await svc.store("u1", "s1", b"<html>")
        with pytest.raises(InvalidRequest):
            await svc.store("u1", "s1", b"")
        with pytest.raises(PayloadTooLarge):
            await svc.store("u1", "s1", JPEG + b"0" * svc.MAX_BYTES)
        with pytest.raises(NotFound):
            await svc.store("u1", "sx", JPEG)  # someone else's session
        return await svc.store("u1", "s1", _png())

    doc = _run(go, tmp_path)
    assert doc.content_type == "image/png" and doc.message_id is None


def test_get_is_owner_and_session_scoped(tmp_path):
    async def go():
        doc = await svc.store("u1", "s1", JPEG)
        got = await svc.get("u1", "s1", doc.id)
        for user, session in (("u2", "s1"), ("u1", "s2")):
            with pytest.raises(NotFound):
                await svc.get(user, session, doc.id)
        return got

    assert _run(go, tmp_path).data == JPEG


def test_claim_validates_ids_and_order_and_single_use(tmp_path):
    async def go():
        a = await svc.store("u1", "s1", JPEG)
        b = await svc.store("u1", "s1", _png())
        with pytest.raises(InvalidRequest):
            await svc.claim("u1", "s1", ["nope"])
        claimed = await svc.claim("u1", "s1", [b.id, a.id, b.id])
        return a, b, claimed

    a, b, claimed = _run(go, tmp_path)
    assert [c.doc.id for c in claimed] == [b.id, a.id]  # order kept, repeat ignored


def test_start_turn_attaches_photos_sends_them_and_marks_them_used(tmp_path):
    class Backend:
        def __init__(self):
            self.images = None

        async def start_run(self, *, session_id, history, user_message, images=()):
            self.images = list(images)
            return "run_x"

        async def stream_events(self, run_id, *, session_id):
            yield RunCompleted(session_id=session_id, run_id=run_id, output="ok")

    backend = Backend()

    async def go():
        doc = await svc.store("u1", "s1", _png())
        photos = await svc.claim("u1", "s1", [doc.id])
        msg = await runs.start_turn(backend, "s1", "what is this?", attachments=photos)
        await asyncio.sleep(0.05)  # let the background reader finish
        stored = (await messages_db.find_by_session("s1"))[0]
        with pytest.raises(InvalidRequest):  # now used — can't be sent twice
            await svc.claim("u1", "s1", [doc.id])
        return doc, msg, stored

    doc, msg, stored = _run(go, tmp_path)
    assert backend.images == [ImageInput("image/png", _png())]
    assert [a.id for a in stored.attachments] == [doc.id]
    out = MessageOut.model_validate(stored)
    assert out.attachments[0].url == f"sessions/s1/attachments/{doc.id}"


def test_message_needs_text_or_a_photo():
    with pytest.raises(ValueError):
        MessageCreate(content="   ")
    assert MessageCreate(content="", attachment_ids=["a"]).attachment_ids == ["a"]
    with pytest.raises(ValueError):
        MessageCreate(content="x", attachment_ids=list("abcde"))  # max 4


def test_photo_only_messages_are_labeled(tmp_path):
    from gaca_api.models.message import AttachmentMeta, MessageDoc
    from gaca_api.services.sessions import labels_for

    photo = MessageDoc(session_id="s", role="user", content="",
                       attachments=[AttachmentMeta(id="a", content_type="image/png", size=1)])
    labels = labels_for(None, photo, photo)
    assert (labels.summary, labels.preview) == ("Photo", "You: Photo")
