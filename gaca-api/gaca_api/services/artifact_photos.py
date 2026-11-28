"""Photos a page shows. No FastAPI, no HTTP.

A photo gets in from outside the page's own code: the app uploads one the person picked or took (after the page
asked with `app.photos.add()`, on a tap), or the agent adds one (a file, or a photo sent in the chat). Either way
the answer is an id, and the PAGE decides where that id goes in its own data (`meals/tacos.photo`, a gallery
collection, ...). The page reads the bytes back through the bridge (`photo.get`), since it has no network.

Every photo is decoded and re-encoded here, so what is stored is a plain image at most FULL_EDGE px on its long
side, plus a THUMB_EDGE thumbnail, with no EXIF (no location, no camera details) whatever was uploaded.

Nothing tells us when a page stops using a photo (its data is the page's business), so `sweep` removes photos
that no state or document of their page mentions any more, once they are older than UNUSED_GRACE.
"""
from __future__ import annotations

import asyncio
import base64
import io
from dataclasses import dataclass
from datetime import timedelta

from PIL import Image, ImageOps, UnidentifiedImageError

from ..db import artifact_photos as db
from ..db import attachments as attachments_db
from ..models.artifact_photo import ArtifactPhotoDoc
from ..models.base import utcnow
from ..schemas import artifact as s
from . import artifacts as artifact_service
from .attachments import sniff
from .exceptions import InvalidRequest, NotFound, PayloadTooLarge

MAX_UPLOAD_BYTES = 15 * 1024 * 1024  # a full-size phone photo; it is shrunk on the way in (see security.py)
MAX_PIXELS = 60_000_000  # refuse decompression bombs before decoding
FULL_EDGE = 2048
THUMB_EDGE = 480
JPEG_QUALITY = 85
MAX_PHOTOS_PER_PAGE = 500
MAX_BYTES_PER_PAGE = 200 * 1024 * 1024
UNUSED_GRACE = timedelta(days=7)


@dataclass(frozen=True)
class Encoded:
    content_type: str
    data: bytes
    width: int
    height: int


@dataclass(frozen=True)
class Processed:
    full: Encoded
    thumb: Encoded


def _encode(image: Image.Image, edge: int, alpha: bool) -> Encoded:
    copy = image.copy()
    copy.thumbnail((edge, edge), Image.Resampling.LANCZOS)  # only ever shrinks
    out = io.BytesIO()
    # No `exif=` and no other metadata: the saved file carries none of the original's.
    if alpha:
        copy.save(out, "WEBP", quality=JPEG_QUALITY, method=4)
        content_type = "image/webp"
    else:
        copy.save(out, "JPEG", quality=JPEG_QUALITY, optimize=True, progressive=True)
        content_type = "image/jpeg"
    return Encoded(content_type, out.getvalue(), copy.width, copy.height)


def process(data: bytes) -> Processed:
    """Decode an uploaded photo and re-encode it at two sizes. CPU work: run it in a thread."""
    if not data:
        raise InvalidRequest("empty upload")
    if len(data) > MAX_UPLOAD_BYTES:
        raise PayloadTooLarge(f"photo is over the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit")
    if sniff(data) is None:
        raise InvalidRequest("unsupported image type (use JPEG, PNG, WebP or GIF)")
    try:
        with Image.open(io.BytesIO(data)) as opened:
            if opened.width * opened.height > MAX_PIXELS:
                raise InvalidRequest("photo has too many pixels")
            opened.seek(0)  # an animated GIF keeps its first frame
            image = ImageOps.exif_transpose(opened)  # stand it the right way up before the EXIF goes
            alpha = image.mode in ("RGBA", "LA", "PA") or (image.mode == "P" and "transparency" in image.info)
            image = image.convert("RGBA" if alpha else "RGB")
            return Processed(_encode(image, FULL_EDGE, alpha), _encode(image, THUMB_EDGE, alpha))
    except InvalidRequest:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError, SyntaxError):
        raise InvalidRequest("the photo could not be read (damaged or not really an image)") from None


def _out(doc: ArtifactPhotoDoc) -> s.PhotoOut:
    return s.PhotoOut(id=doc.id, content_type=doc.content_type, width=doc.width, height=doc.height, size=doc.size,
                      added_by=doc.added_by, created_at=doc.created_at)


async def add(user_id: str, artifact_id: str, data: bytes, added_by: str) -> s.PhotoOut:
    await artifact_service._owned_w(user_id, artifact_id)  # noqa: SLF001 - same ownership and move rules as data
    count, used = await db.usage(artifact_id)
    if count >= MAX_PHOTOS_PER_PAGE:
        raise InvalidRequest(f"this page already has {MAX_PHOTOS_PER_PAGE} photos; remove some first")
    processed = await asyncio.to_thread(process, data)
    if used + len(processed.full.data) + len(processed.thumb.data) > MAX_BYTES_PER_PAGE:
        raise PayloadTooLarge(f"this page's photos would pass {MAX_BYTES_PER_PAGE // (1024 * 1024)} MB; remove some first")
    full, thumb = processed.full, processed.thumb
    doc = ArtifactPhotoDoc(
        artifact_id=artifact_id, content_type=full.content_type, width=full.width, height=full.height,
        size=len(full.data), thumb_content_type=thumb.content_type, thumb_width=thumb.width,
        thumb_height=thumb.height, thumb_size=len(thumb.data), added_by=added_by,
    )
    await db.insert(doc, full.data, thumb.data)
    await sweep(artifact_id)
    return _out(doc)


async def add_from_attachment(user_id: str, artifact_id: str, attachment_id: str, added_by: str) -> s.PhotoOut:
    """Put a photo that was sent in a chat onto a page (the agent's way to do "add this to the tacos meal")."""
    found = await attachments_db.find(attachment_id)
    if found is None or found[0].user_id != user_id:
        raise NotFound("attachment", attachment_id)  # 404 either way: don't confirm someone else's id exists
    return await add(user_id, artifact_id, found[1], added_by)


async def list_photos(user_id: str, artifact_id: str) -> s.PhotoList:
    await artifact_service._owned(user_id, artifact_id)  # noqa: SLF001
    return s.PhotoList(photos=[_out(d) for d in await db.list_for(artifact_id)])


async def get(user_id: str, artifact_id: str, photo_id: str, *, thumb: bool = False) -> tuple[ArtifactPhotoDoc, bytes, str]:
    """(metadata, bytes, content type) of one photo or its thumbnail."""
    await artifact_service._owned(user_id, artifact_id)  # noqa: SLF001
    found = await db.find(artifact_id, photo_id, thumb=thumb)
    if found is None:
        raise NotFound("photo", photo_id)
    doc, data = found
    return doc, data, doc.thumb_content_type if thumb else doc.content_type


async def get_for_page(user_id: str, artifact_id: str, photo_id: str, *, thumb: bool) -> s.PhotoData:
    """What the page's `photo.get` call answers: the bytes as base64, for the SDK to turn into a blob: URL."""
    doc, data, content_type = await get(user_id, artifact_id, photo_id, thumb=thumb)
    return s.PhotoData(
        id=doc.id, content_type=content_type, data=base64.b64encode(data).decode("ascii"),
        width=doc.thumb_width if thumb else doc.width, height=doc.thumb_height if thumb else doc.height,
    )


async def delete(user_id: str, artifact_id: str, photo_id: str) -> None:
    await artifact_service._owned_w(user_id, artifact_id)  # noqa: SLF001
    if not await db.delete(artifact_id, [photo_id]):
        raise NotFound("photo", photo_id)


async def count(artifact_id: str) -> int:
    return (await db.usage(artifact_id))[0]


async def sweep(artifact_id: str) -> int:
    """Remove this page's photos that are past the grace period and no longer mentioned anywhere in its data."""
    old = await db.older_than(artifact_id, utcnow() - UNUSED_GRACE)
    if not old:
        return 0
    text = await db.data_text(artifact_id)
    return await db.delete(artifact_id, [pid for pid in old if pid not in text])
