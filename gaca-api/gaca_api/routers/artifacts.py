"""HTTP layer for artifacts. Thin: validate, call the service, return. Every route needs
auth and only ever sees the caller's own artifacts."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Path, Query
from fastapi.responses import HTMLResponse

from ..models.user import UserDoc
from ..schemas import artifact as s
from ..services import artifact_page
from ..services import artifacts as svc
from ..utils.auth import get_current_user

router = APIRouter(prefix="/artifacts", tags=["artifacts"])

_COLLECTION = Path(pattern=s.NAME_PATTERN, description="Collection name, e.g. 'items'")
_DOC_ID = Path(pattern=s.DOC_ID_PATTERN)


@router.post("", status_code=201)
async def create_artifact(body: s.ArtifactCreate, user: UserDoc = Depends(get_current_user)) -> s.ArtifactOut:
    """Publish an HTML page, optionally seeded with data. To show it in a chat, include the
    returned `link` (gaca://artifact/<id>) in your reply; to show it on an alert, pass
    `alert_id` here. It also appears in the app's Artifacts tab."""
    return await svc.create(user.id, body)


@router.get("")
async def list_artifacts(
    include_archived: bool = False,
    session_id: str | None = Query(default=None, description="Only artifacts made for this chat"),
    alert_id: str | None = Query(default=None, description="Only artifacts made for this alert"),
    q: str | None = Query(default=None, description="Match against title / description"),
    limit: int = Query(default=100, ge=1, le=500),
    user: UserDoc = Depends(get_current_user),
) -> s.ArtifactList:
    """Your artifacts in your own order (see PUT /order). Pages you haven't placed yet come
    first, most recently changed first."""
    return await svc.list_for_user(user.id, include_archived, session_id, alert_id, q, limit)


@router.put("/order")
async def reorder_artifacts(body: s.ArtifactOrder, user: UserDoc = Depends(get_current_user)) -> s.ArtifactList:
    """Save the order the owner dragged their artifacts into. Listed ids come first, in the
    order given; the rest follow in their existing order. Returns the (non-archived) list."""
    return await svc.reorder(user.id, body.ids)


@router.get("/{artifact_id}")
async def get_artifact(artifact_id: str, user: UserDoc = Depends(get_current_user)) -> s.ArtifactOut:
    return await svc.get(user.id, artifact_id)


@router.get("/{artifact_id}/html", response_class=HTMLResponse)
async def get_html(
    artifact_id: str,
    version: int | None = Query(default=None, ge=1),
    user: UserDoc = Depends(get_current_user),
) -> HTMLResponse:
    """The page itself (text/html). Pass `version` for an older one."""
    html = await svc.get_html(user.id, artifact_id, version)
    return HTMLResponse(
        html,
        headers={
            "Content-Security-Policy": "sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline'; "
            "style-src 'unsafe-inline'; img-src data: blob:; font-src data:; media-src data: blob:",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )


@router.get("/{artifact_id}/page", response_class=HTMLResponse)
async def get_page(artifact_id: str, user: UserDoc = Depends(get_current_user)) -> HTMLResponse:
    """The page ready to show in an app's web view: the current HTML wrapped with the
    network-blocking CSP, viewport, theme CSS and the `window.app` SDK. Load it as-is; route
    the SDK's bridge calls to POST /{artifact_id}/call."""
    html = await artifact_page.page(user.id, artifact_id)
    return HTMLResponse(
        html,
        headers={
            "Content-Security-Policy": artifact_page.CSP,
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )


@router.post("/{artifact_id}/call")
async def call_from_page(
    artifact_id: str, body: s.ArtifactCall, user: UserDoc = Depends(get_current_user)
) -> Any:
    """Perform one of the page's `window.app.*` data calls (`data`, `state.get|set|patch`,
    `doc.list|add|set|patch|remove|clear`, `batch`, `emit`). Answers exactly what the matching
    endpoint above would — same JSON, same errors (`null` where that endpoint sends 204).
    UI calls (`ask`, `openUrl`, `toast`) belong to the client: `400 unknown method`."""
    return await artifact_page.call(user.id, artifact_id, body.method, body.args)


@router.patch("/{artifact_id}")
async def update_artifact(
    artifact_id: str, body: s.ArtifactPatch, user: UserDoc = Depends(get_current_user)
) -> s.ArtifactOut:
    """Retitle, archive, or replace the page (`html` makes a new version; the stored data
    is never touched, so a rewrite must keep reading the same data shape)."""
    return await svc.patch(user.id, artifact_id, body)


@router.delete("/{artifact_id}", status_code=204)
async def delete_artifact(artifact_id: str, user: UserDoc = Depends(get_current_user)) -> None:
    """Delete the page and everything it stored. (Archive instead to just tidy it away.)"""
    await svc.delete(user.id, artifact_id)


@router.post("/{artifact_id}/opened", status_code=204)
async def mark_opened(artifact_id: str, user: UserDoc = Depends(get_current_user)) -> None:
    """The app calls this when the user opens a page (for 'recently used' ordering)."""
    await svc.mark_opened(user.id, artifact_id)


@router.get("/{artifact_id}/versions")
async def list_versions(artifact_id: str, user: UserDoc = Depends(get_current_user)) -> s.VersionList:
    return await svc.versions(user.id, artifact_id)


@router.post("/{artifact_id}/restore")
async def restore_version(
    artifact_id: str, body: s.Restore, user: UserDoc = Depends(get_current_user)
) -> s.ArtifactOut:
    """Bring an old version back as a new one."""
    return await svc.restore(user.id, artifact_id, body.version)


# ── data: one state document ─────────────────────────────────────────────────
@router.get("/{artifact_id}/data")
async def get_data(
    artifact_id: str,
    since_rev: int | None = Query(default=None, ge=0, description="The `rev` you last saw"),
    user: UserDoc = Depends(get_current_user),
) -> s.DataSnapshot:
    """Everything the page stores (state + every collection) in one read. Pass `since_rev`
    to poll: if nothing changed you get `{rev, changed: false}` and nothing else."""
    return await svc.snapshot(user.id, artifact_id, since_rev)


@router.get("/{artifact_id}/state")
async def get_state(artifact_id: str, user: UserDoc = Depends(get_current_user)) -> s.StateOut:
    return await svc.get_state(user.id, artifact_id)


@router.put("/{artifact_id}/state")
async def put_state(artifact_id: str, body: s.StatePut, user: UserDoc = Depends(get_current_user)) -> s.StateOut:
    """Replace the whole state. With `expected_version`, `409` if someone else wrote first."""
    return await svc.put_state(user.id, artifact_id, body)


@router.patch("/{artifact_id}/state")
async def patch_state(artifact_id: str, body: s.StatePatch, user: UserDoc = Depends(get_current_user)) -> s.StateOut:
    """Merge into the state (JSON merge patch: objects merge, `null` deletes a key, arrays replace)."""
    return await svc.patch_state(user.id, artifact_id, body)


# ── data: keyed collections ──────────────────────────────────────────────────
@router.post("/{artifact_id}/batch")
async def batch(artifact_id: str, body: s.Batch, user: UserDoc = Depends(get_current_user)) -> s.BatchResult:
    """Apply several document writes (`set`, `patch`, `delete`, `clear`) all-or-nothing."""
    return await svc.batch(user.id, artifact_id, body)


@router.get("/{artifact_id}/collections/{collection}")
async def list_collection(
    artifact_id: str, collection: str = _COLLECTION, user: UserDoc = Depends(get_current_user)
) -> s.CollectionOut:
    """Every document in a collection, oldest first (an empty list if there are none yet)."""
    return await svc.list_collection(user.id, artifact_id, collection)


@router.post("/{artifact_id}/collections/{collection}", status_code=201)
async def add_doc(
    artifact_id: str, body: s.DocCreate, collection: str = _COLLECTION, user: UserDoc = Depends(get_current_user)
) -> s.DocOut:
    """Add a document with a generated id (or your own `id`; `409` if taken)."""
    return await svc.add_doc(user.id, artifact_id, collection, body)


@router.delete("/{artifact_id}/collections/{collection}", status_code=204)
async def clear_collection(
    artifact_id: str, collection: str = _COLLECTION, user: UserDoc = Depends(get_current_user)
) -> None:
    await svc.clear_collection(user.id, artifact_id, collection)


@router.get("/{artifact_id}/collections/{collection}/{doc_id}")
async def get_doc(
    artifact_id: str, collection: str = _COLLECTION, doc_id: str = _DOC_ID, user: UserDoc = Depends(get_current_user)
) -> s.DocOut:
    return await svc.get_doc(user.id, artifact_id, collection, doc_id)


@router.put("/{artifact_id}/collections/{collection}/{doc_id}")
async def put_doc(
    artifact_id: str, body: s.DocPut, collection: str = _COLLECTION, doc_id: str = _DOC_ID,
    user: UserDoc = Depends(get_current_user),
) -> s.DocOut:
    """Create or replace a document. With `expected_version`, `409` if it changed meanwhile."""
    return await svc.put_doc(user.id, artifact_id, collection, doc_id, body)


@router.patch("/{artifact_id}/collections/{collection}/{doc_id}")
async def patch_doc(
    artifact_id: str, body: s.DocPatch, collection: str = _COLLECTION, doc_id: str = _DOC_ID,
    user: UserDoc = Depends(get_current_user),
) -> s.DocOut:
    """Merge into an existing document (JSON merge patch). `404` if it doesn't exist."""
    return await svc.patch_doc(user.id, artifact_id, collection, doc_id, body)


@router.delete("/{artifact_id}/collections/{collection}/{doc_id}", status_code=204)
async def delete_doc(
    artifact_id: str, collection: str = _COLLECTION, doc_id: str = _DOC_ID, user: UserDoc = Depends(get_current_user)
) -> None:
    await svc.delete_doc(user.id, artifact_id, collection, doc_id)


# ── events: the page telling the agent something happened ────────────────────
@router.post("/{artifact_id}/events", status_code=201)
async def add_event(artifact_id: str, body: s.EventIn, user: UserDoc = Depends(get_current_user)) -> s.EventOut:
    """What the page's `app.emit(name, payload)` calls. Agents read these to react."""
    return await svc.add_event(user.id, artifact_id, body)


@router.get("/{artifact_id}/events")
async def list_events(
    artifact_id: str,
    after: int = Query(default=0, ge=0, description="Only events after this seq"),
    unhandled: bool = Query(default=False, description="Only events not yet marked handled"),
    limit: int = Query(default=100, ge=1, le=500),
    user: UserDoc = Depends(get_current_user),
) -> s.EventList:
    return await svc.list_events(user.id, artifact_id, after, unhandled, limit)


@router.post("/{artifact_id}/events/handled")
async def mark_handled(
    artifact_id: str, body: s.EventsHandled, user: UserDoc = Depends(get_current_user)
) -> s.HandledResult:
    """Mark events up to `upto` as handled, so the next `unhandled` read skips them."""
    return await svc.mark_events_handled(user.id, artifact_id, body.upto)
