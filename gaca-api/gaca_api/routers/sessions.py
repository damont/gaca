"""HTTP layer for sessions + their messages.

Routes are thin: validate the request schema, call a service, map the returned
DB model to a response schema. No business logic, no DB access here.

Every route requires auth and is scoped to the caller — this predates the
alerting build (original chat feature, no user concept at all) and was
retrofitted here alongside per-user crew credentials.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sse_starlette.sse import EventSourceResponse

from ..models.user import UserDoc
from ..schemas.message import MessageAccepted, MessageCreate, MessageOut
from ..schemas.session import SessionCreate, SessionDetail, SessionList, SessionOut
from ..services import auth as auth_service
from ..services import events as events_service
from ..services import runs as run_service
from ..services import sessions as session_service
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
                update={"summary": labels.summary, "preview": labels.preview}
            )
            for s, labels in rows
        ]
    )


@router.get("/{session_id}")
async def get_session(
    session_id: str, user: UserDoc = Depends(get_current_user)
) -> SessionDetail:
    session, messages, labels = await session_service.get_with_messages(
        user.id, session_id
    )
    return SessionDetail(
        **SessionOut.model_validate(session).model_dump(exclude={"summary", "preview"}),
        summary=labels.summary,
        preview=labels.preview,
        messages=[MessageOut.model_validate(m) for m in messages],
    )


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
    message = await run_service.start_turn(
        backend,
        session_id,
        body.content,
        on_settled=run_service.chat_reply_notifier(user.id, session_id, crew_member.name),
    )
    return MessageAccepted(turn_id=message.id)


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
