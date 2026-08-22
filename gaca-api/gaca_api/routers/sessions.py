"""HTTP layer for sessions + their messages.

Routes are thin: validate the request schema, call a service, map the returned
DB model to a response schema. No business logic, no DB access here.

Every route requires auth and is scoped to the caller — this predates the
alerting build (original chat feature, no user concept at all) and was
retrofitted here alongside per-user agent credentials.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sse_starlette.sse import EventSourceResponse

from ..agent import runtime as agent_runtime
from ..models.user import UserDoc
from ..schemas.message import MessageAccepted, MessageCreate, MessageOut
from ..schemas.session import SessionCreate, SessionDetail, SessionOut
from ..services import events as events_service
from ..services import runs as run_service
from ..services import sessions as session_service
from ..utils.auth import get_current_user

router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.post("", status_code=201)
async def create_session(
    body: SessionCreate, user: UserDoc = Depends(get_current_user)
) -> SessionOut:
    session = await session_service.create(user.id, body.title)
    return SessionOut.model_validate(session)


@router.get("/{session_id}")
async def get_session(
    session_id: str, user: UserDoc = Depends(get_current_user)
) -> SessionDetail:
    session, messages = await session_service.get_with_messages(user.id, session_id)
    return SessionDetail(
        **SessionOut.model_validate(session).model_dump(),
        messages=[MessageOut.model_validate(m) for m in messages],
    )


@router.post("/{session_id}/messages", status_code=202)
async def post_message(
    session_id: str,
    body: MessageCreate,
    user: UserDoc = Depends(get_current_user),
) -> MessageAccepted:
    await session_service.ensure_owned(user.id, session_id)
    backend = agent_runtime.backend_for(user.agent_base_url, user.agent_api_key)
    message = await run_service.start_turn(backend, session_id, body.content)
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
