"""HTTP layer for sessions + their messages.

Routes are thin: validate the request schema, call a service, map the returned
DB model to a response schema. No business logic, no DB access here.
"""
from __future__ import annotations

from fastapi import APIRouter
from sse_starlette.sse import EventSourceResponse

from ..schemas.message import MessageAccepted, MessageCreate, MessageOut
from ..schemas.session import SessionCreate, SessionDetail, SessionOut
from ..services import events as events_service
from ..services import messages as message_service
from ..services import sessions as session_service

router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.post("", status_code=201)
async def create_session(body: SessionCreate) -> SessionOut:
    session = await session_service.create(body.title)
    return SessionOut.model_validate(session)


@router.get("/{session_id}")
async def get_session(session_id: str) -> SessionDetail:
    session, messages = await session_service.get_with_messages(session_id)
    return SessionDetail(
        **SessionOut.model_validate(session).model_dump(),
        messages=[MessageOut.model_validate(m) for m in messages],
    )


@router.post("/{session_id}/messages", status_code=202)
async def post_message(session_id: str, body: MessageCreate) -> MessageAccepted:
    message = await message_service.add(session_id, body.role, body.content)
    return MessageAccepted(turn_id=message.id)


@router.get("/{session_id}/events")
async def session_events(session_id: str) -> EventSourceResponse:
    # check existence up front: once we return a stream the status is already
    # 200, so a 404 has to happen before streaming starts.
    await session_service.ensure_exists(session_id)
    return EventSourceResponse(events_service.stream(session_id))
