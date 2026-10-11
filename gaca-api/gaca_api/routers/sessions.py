"""HTTP layer for sessions + their messages.

Routes are thin: validate the request schema, call a service, map the returned
DB model to a response schema. No business logic, no DB access here.

Every route requires auth and is scoped to the caller — this predates the
alerting build (original chat feature, no user concept at all) and was
retrofitted here alongside per-user crew credentials.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from sse_starlette.sse import EventSourceResponse

from ..models.user import UserDoc
from ..schemas.message import MessageAccepted, MessageCreate
from ..schemas.session import SessionCreate, SessionDetail, SessionList, SessionOut
from ..schemas.message import AttachmentOut
from ..services import attachments as attachment_service
from ..services import auth as auth_service
from ..services import events as events_service
from ..services import runs as run_service
from ..services import sessions as session_service
from ..services.exceptions import PayloadTooLarge
from ..utils.auth import get_current_user

router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.post("", status_code=201)
async def create_session(
    body: SessionCreate, user: UserDoc = Depends(get_current_user)
) -> SessionOut:
    crew_member = auth_service.resolve_crew_member(user, body.crew)
    session = await session_service.create(user.id, body.title, crew_member.name)
    return SessionOut.model_validate(session)


@router.get("")
async def list_sessions(user: UserDoc = Depends(get_current_user)) -> SessionList:
    """The Chat tab: this user's sessions, most recently active first, each labeled
    with a summary and last-message preview (see services/sessions.py:labels_for)."""
    rows = await session_service.list_for_user(user.id)
    return SessionList(
        sessions=[
            SessionOut.model_validate(s).model_copy(
                update={
                    "summary": labels.summary,
                    "preview": labels.preview,
                    "last_activity_at": labels.last_activity_at or s.created_at,
                }
            )
            for s, labels in rows
        ]
    )


@router.get("/{session_id}")
async def get_session(
    session_id: str, user: UserDoc = Depends(get_current_user)
) -> SessionDetail:
    return await session_service.get_detail(user.id, session_id)


@router.post("/{session_id}/messages", status_code=202)
async def post_message(
    session_id: str,
    body: MessageCreate,
    user: UserDoc = Depends(get_current_user),
) -> MessageAccepted:
    session = await session_service.ensure_owned(user.id, session_id)
    # `session.crew` is unset only for a session created before crew members
    # existed; fall back to the account default the same way a fresh session
    # would have resolved it, rather than 400ing on old data.
    crew_member = auth_service.find_crew_member(user, session.crew)
    if crew_member is None:
        crew_member = auth_service.resolve_crew_member(user, None)
    backend = auth_service.backend_for_crew(user, crew_member)
    photos = await attachment_service.claim(user.id, session_id, body.attachment_ids)
    message = await run_service.start_turn(
        backend,
        session_id,
        body.content,
        on_settled=run_service.chat_reply_notifier(user.id, session_id, crew_member.name),
        attachments=photos,
    )
    return MessageAccepted(turn_id=message.id)


@router.post(
    "/{session_id}/attachments",
    status_code=201,
    openapi_extra={
        "requestBody": {
            "required": True,
            "description": "The raw image bytes (JPEG, PNG, WebP or GIF; max 2 MB).",
            "content": {"image/*": {"schema": {"type": "string", "format": "binary"}}},
        }
    },
)
async def upload_attachment(
    session_id: str, request: Request, user: UserDoc = Depends(get_current_user)
) -> AttachmentOut:
    """Upload one photo to attach to a later message in this session. The body is
    the raw image; send the returned `id` in `attachment_ids` of the message."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > attachment_service.MAX_BYTES:
        # Reject before reading a body we'd only throw away.
        raise PayloadTooLarge("photo is over the 2 MB limit")
    doc = await attachment_service.store(user.id, session_id, await request.body())
    return AttachmentOut(
        id=doc.id,
        content_type=doc.content_type,
        size=doc.size,
        url=f"sessions/{session_id}/attachments/{doc.id}",
    )


@router.get("/{session_id}/attachments/{attachment_id}")
async def get_attachment(
    session_id: str, attachment_id: str, user: UserDoc = Depends(get_current_user)
) -> Response:
    """A photo's bytes. Same auth as everything else — it's private to the owner."""
    stored = await attachment_service.get(user.id, session_id, attachment_id)
    return Response(
        content=stored.data,
        media_type=stored.doc.content_type,
        headers={"Cache-Control": "private, max-age=31536000, immutable"},
    )


@router.get("/{session_id}/events")
async def session_events(
    session_id: str, request: Request, user: UserDoc = Depends(get_current_user)
) -> EventSourceResponse:
    # check ownership up front: once we return a stream the status is already
    # 200, so a 404 has to happen before streaming starts.
    await session_service.ensure_owned(user.id, session_id)
    # On reconnect the browser resends the last id it saw; replay from there.
    raw = request.headers.get("last-event-id")
    last_event_id = int(raw) if raw and raw.isdigit() else 0
    return EventSourceResponse(events_service.stream(session_id, last_event_id))
