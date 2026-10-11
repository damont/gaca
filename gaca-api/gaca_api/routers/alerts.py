"""HTTP layer for alerts.

Routes are thin: validate the request schema, call a service, map the returned
DB model to a response schema. No business logic, no DB access here.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response

from ..models.alert import AlertDoc
from ..models.user import UserDoc
from ..schemas.alert import (
    AlertCreate,
    AlertDelegate,
    AlertList,
    AlertOut,
    AlertResolve,
    AlertSnooze,
    AlertTakeToChat,
    DelegationCreate,
    TakeToChatOut,
)
from ..services import alerts as alert_service
from ..utils.auth import get_current_user

router = APIRouter(prefix="/alerts", tags=["alerts"])


async def _out(alert: AlertDoc) -> AlertOut:
    """Map a stored alert to the client shape (see alert_service.to_out)."""
    [out] = await alert_service.to_out(alert.user_id, [alert])
    return out


@router.post("", response_model=AlertOut)
async def create_alert(
    body: AlertCreate,
    response: Response,
    user: UserDoc = Depends(get_current_user),
):
    """Post an alert. This is what an agent calls.

    201 when a new alert was created, 200 when it deduped onto an existing one —
    so a caller can tell whether it actually told the user something new.
    """
    alert, created = await alert_service.create(user.id, body)
    response.status_code = 201 if created else 200
    return await _out(alert)


@router.post("/delegations", response_model=AlertOut, status_code=201)
async def create_delegation(body: DelegationCreate, user: UserDoc = Depends(get_current_user)):
    """"Have a crew member do this." Raises a `manual.delegation` alert for the instruction
    and delegates it in one call; returns it already `delegated`. The run settles it like
    any delegation."""
    return await _out(
        await alert_service.delegate_new(user, body.instruction, crew=body.crew, source=body.source)
    )


@router.get("", response_model=AlertList)
async def list_alerts(
    user: UserDoc = Depends(get_current_user),
    # Repeatable: ?status=new&status=snoozed. Filtered on EFFECTIVE status, so
    # an elapsed snooze shows up under `new`.
    status: Annotated[list[str] | None, Query()] = None,
    severity: Annotated[list[str] | None, Query()] = None,
    crew: Annotated[list[str] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
):
    """The inbox: this user's alerts, newest first."""
    alerts = await alert_service.list_for_user(
        user.id, statuses=status, severities=severity, crews=crew, limit=limit
    )
    return AlertList(alerts=await alert_service.to_out(user.id, alerts))


@router.get("/{alert_id}", response_model=AlertOut)
async def get_alert(alert_id: str, user: UserDoc = Depends(get_current_user)):
    return await _out(await alert_service.get(user.id, alert_id))


@router.post("/{alert_id}/act", response_model=AlertOut)
async def act_on_alert(
    alert_id: str,
    body: AlertResolve | None = None,
    user: UserDoc = Depends(get_current_user),
):
    """"I handled it myself." Terminal."""
    return await _out(await alert_service.act(user.id, alert_id, body.note if body else None))


@router.post("/{alert_id}/dismiss", response_model=AlertOut)
async def dismiss_alert(
    alert_id: str,
    body: AlertResolve | None = None,
    user: UserDoc = Depends(get_current_user),
):
    """"Ignore this." Terminal — though a re-fired dedupe_key resurfaces it."""
    return await _out(
        await alert_service.dismiss(user.id, alert_id, body.note if body else None)
    )


@router.post("/{alert_id}/snooze", response_model=AlertOut)
async def snooze_alert(
    alert_id: str,
    body: AlertSnooze,
    user: UserDoc = Depends(get_current_user),
):
    """"Not now." Reads as `new` again once `until` passes."""
    return await _out(await alert_service.snooze(user.id, alert_id, body.until))


@router.post("/{alert_id}/take-to-chat", response_model=TakeToChatOut)
async def take_alert_to_chat(
    alert_id: str,
    body: AlertTakeToChat | None = None,
    user: UserDoc = Depends(get_current_user),
):
    """"Talk to Hermes about this." Opens the chat and returns a `draft` to pre-fill —
    nothing is sent until the person sends it — and marks the alert handled (`acted`)."""
    b = body or AlertTakeToChat()
    alert, session_id, draft = await alert_service.take_to_chat(
        user, alert_id, suggested_action_index=b.suggested_action_index, crew=b.crew
    )
    return TakeToChatOut(alert=await _out(alert), session_id=session_id, draft=draft)


@router.post("/{alert_id}/delegate", response_model=AlertOut)
async def delegate_alert(
    alert_id: str,
    body: AlertDelegate | None = None,
    user: UserDoc = Depends(get_current_user),
):
    """"Hand it to Hermes." Starts a real run; the alert settles once it ends."""
    b = body or AlertDelegate()
    return await _out(
        await alert_service.delegate(
            user,
            alert_id,
            instruction=b.instruction,
            suggested_action_index=b.suggested_action_index,
            crew=b.crew,
        )
    )
