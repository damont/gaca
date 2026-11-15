"""Publishing a page to the template gallery, for the app: ONE pair of routes that works whether the page is
private (on this server) or already in the cloud. Kept apart from sharing (services/cloud_share.py): publishing
and sharing do not share a service, a route or a rule.

  * private page: this server SENDS the page's current html and collection shapes (never the data, the state,
    the events or any document version) to the cloud's `POST /templates/preview` and `POST /templates/publish`,
    with `source.id` = the local artifact id. Publishing the same page again makes a new VERSION of the same
    template. The template id is remembered on the local page (`published_template_id`).
  * cloud page: the same listing is forwarded to the cloud's own gacabit routes (`/gacabits/{id}/validations`
    with subject publish, and `/gacabits/{id}/publish`). The cloud already knows its template id.

The app never needs to know which one it is. Both routes are for a PAIRED DEVICE only (the person's tap in the
app is the confirmation), so this layer sets `user_confirmed: true` itself and never takes it from the request.
The forwarder stays dumb: the cloud's answers and errors (a `preview_mismatch` 409, a blocked scan, 402, 429)
go back unchanged. What this layer adds: the local preconditions below, and remembering the template id.

Refused here, before anything is sent: not linked / cloud not configured / read-only link, an archived page, a
page in the middle of a move or bring-home.
"""
from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..config import settings
from ..db import artifacts as db
from ..db import cloud as cloud_db
from ..models.cloud import CloudLink
from . import artifact_locks, cloud_client, cloud_link, cloud_list
from . import artifacts as artifact_service
from .exceptions import ArtifactMoving, CloudError, NotFound

_JSON = {"content-type": "application/json"}


class Listing(BaseModel):
    """The listing text. The cloud checks lengths, tags, category and license; this layer only refuses fields it
    does not know (so the app cannot smuggle `source`, `user_confirmed` or data through)."""
    model_config = ConfigDict(extra="forbid")
    title: str | None = None
    description: str | None = None
    include_data_notes: bool = False
    tags: list[str] = Field(default_factory=list)
    category: str | None = None
    license: str | None = None


class PublishBody(Listing):
    visibility: str = "listed"
    confirmed_preview: bool = False
    preview_hash: str = ""


def _listing(body: Listing) -> dict[str, Any]:
    return {k: v for k, v in body.model_dump(include=set(Listing.model_fields)).items() if v is not None}


async def _manage_link(user_id: str) -> CloudLink:
    link = await cloud_db.get_link(user_id)
    if link is None and not settings.cloud_url.strip():
        raise CloudError("The cloud is not set up on this server (GACA_CLOUD_URL is empty).",
                         status=409, code="cloud_not_configured")
    link = await cloud_link.require_link(user_id)  # 409 cloud_not_linked / 502 cloud_link_invalid
    if link.scope != "manage":
        raise CloudError("This server's cloud link is read-only. Link it again with full access to publish pages.",
                         status=403, code="cloud_link_read_only")
    return link


async def _where(user_id: str, artifact_id: str) -> tuple[str, db.ArtifactRow | None]:
    """('cloud', None) for a page that lives in the cloud, ('local', row) for a private one (with its html).
    Refuses an archived page or one being moved. 404 for a page this account does not have."""
    pointer = await cloud_db.get_pointer(user_id, artifact_id)
    if artifact_id in artifact_locks.MOVING:
        raise ArtifactMoving(artifact_id)
    if artifact_id in artifact_locks.BUSY:
        raise CloudError("A move or bring-home of this page is running. Wait for it to finish.",
                         status=409, code="transfer_in_progress")
    if pointer is not None:
        if pointer.meta.get("archived"):
            raise CloudError("Archived pages cannot be published. Unarchive it first.", status=409, code="artifact_archived")
        return "cloud", None
    row = await db.find(user_id, artifact_id, with_html=True)
    if row is None:
        raise NotFound("artifact", artifact_id)
    if row.archived:
        raise CloudError("Archived pages cannot be published. Unarchive it first.", status=409, code="artifact_archived")
    return "local", row


def _source(row: db.ArtifactRow, include_data_notes: bool) -> dict[str, Any]:
    """The page as it is published: html and collection shapes. Nothing that is data."""
    source: dict[str, Any] = {"id": row.doc.id, "html": row.html or "", "schemas": row.doc.schemas}
    if include_data_notes and row.doc.data_notes:
        source["data_notes"] = row.doc.data_notes
    return source


def _encode(data: dict[str, Any]) -> None:
    return json.dumps(data).encode()


async def check(user_id: str, artifact_id: str, body: Listing, request_id: str | None) -> None:
    """The checks and the preview the person confirms: `{status, findings, content_hash, preview, ...}`."""
    await _manage_link(user_id)
    where, row = await _where(user_id, artifact_id)
    listing = _listing(body)
    if where == "cloud":
        path, payload = f"/gacabits/{cloud_client.quote_id(artifact_id)}/validations", {**listing, "subject": "publish"}
    else:
        assert row is not None
        listing.setdefault("title", row.doc.title)  # a private page has no gacabit title to fall back on
        path, payload = "/templates/preview", {**listing, "source": _source(row, body.include_data_notes)}
    resp = await cloud_link.call(user_id, "POST", path, content=_encode(payload), headers=_JSON, request_id=request_id)
    raise cloud_link.reply_from(resp)


async def publish(user_id: str, artifact_id: str, body: PublishBody, request_id: str | None) -> None:
    """Publish exactly what was previewed (`confirmed_preview` + `preview_hash`); the cloud refuses a mismatch."""
    await _manage_link(user_id)
    where, row = await _where(user_id, artifact_id)
    payload = {**_listing(body), "visibility": body.visibility, "confirmed_preview": body.confirmed_preview,
               "preview_hash": body.preview_hash, "user_confirmed": True}
    if where == "cloud":
        path = f"/gacabits/{cloud_client.quote_id(artifact_id)}/publish"
    else:
        assert row is not None
        payload.setdefault("title", row.doc.title)
        payload["source"] = _source(row, body.include_data_notes)
        path = "/templates/publish"
    resp = await cloud_link.call(user_id, "POST", path, content=_encode(payload), headers=_JSON, request_id=request_id)
    if where == "local":
        await _remember(user_id, artifact_id, resp.content)
    cloud_list.forget(user_id)  # a cloud page's published_template_id is read from the cloud on the next list
    raise cloud_link.reply_from(resp)


_TEMPLATE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


async def _remember(user_id: str, artifact_id: str, content: bytes) -> None:
    """Store the template id the cloud returned on the local page, so the list can show 'published'. Best effort:
    the template exists either way, so a problem here must not turn a successful publish into an error."""
    try:
        template_id = json.loads(content).get("id")
    except (ValueError, AttributeError):
        return
    if not isinstance(template_id, str) or not _TEMPLATE_ID.match(template_id):
        return
    async with artifact_service._write_lock:  # noqa: SLF001 - a read-modify-write of the doc JSON
        row = await db.find(user_id, artifact_id)
        if row is None or row.doc.published_template_id == template_id:
            return  # moved to the cloud meanwhile, or nothing to change
        doc = row.doc.model_copy(update={"published_template_id": template_id})
        await db.update_meta(doc, row.archived, row.updated_at)  # not a content change: rev and updated_at stay
