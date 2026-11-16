"""HTTP layer for alerts.

Routes are thin: validate the request schema, call a service, map the returned
DB model to a response schema. No business logic, no DB access here.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Response

from ..models.alert import AlertDoc
from ..models.user import UserDoc
from ..schemas.alert import (
    AlertActionRun,
    AlertClose,
    AlertCreate,
    AlertDelegate,
    AlertList,
    AlertOut,
    AlertResolve,
    AlertTakeToChat,
    DelegationCreate,
    TakeToChatOut,
)
from ..services import alerts as alert_service
from ..utils.auth import Caller, get_caller, get_current_user

router = APIRouter(prefix="/alerts", tags=["alerts"])


async def _out(alert: AlertDoc) -> AlertOut:
    """Map a stored alert to the client shape (see alert_service.to_out)."""
    [out] = await alert_service.to_out(alert.user_id, [alert])
    return out


@router.post("", response_model=AlertOut)
async def create_alert(
    body: AlertCreate,
    response: Response,
    caller: Caller = Depends(get_caller),
):
    """Post an alert. This is what an agent calls.

    201 when a new alert was created, 200 when it deduped onto an existing one —
    so a caller can tell whether it actually told the user something new.
    """
    alert, created = await alert_service.create(caller.user.id, body, caller.cloud)
    response.status_code = 201 if created else 200
    return await _out(alert)


@router.post("/delegations", response_model=AlertOut, status_code=201)
async def create_delegation(body: DelegationCreate, user: UserDoc = Depends(get_current_user)):
    """"Have a crew member do this." Raises a `manual.delegation` alert for the instruction
    and runs it in one call; returns it with the run in flight. A successful run closes it
    as `done_by_gaca`."""
    return await _out(
        await alert_service.delegate_new(user, body.instruction, crew=body.crew, source=body.source)
    )


@router.get("", response_model=AlertList)
async def list_alerts(
    user: UserDoc = Depends(get_current_user),
    # Repeatable: ?status=new&status=noted. `new` is the open inbox; the rest are how an alert ended.
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


@router.post("/{alert_id}/close", response_model=AlertOut)
async def close_alert(
    alert_id: str,
    body: AlertClose,
    user: UserDoc = Depends(get_current_user),
):
    """Close an open alert and say how it ended: `done_by_me` (I handled it), `noted` (useful,
    nothing to do) or `not_useful` (I don't want this). `done_by_gaca` is set by the system when
    an action succeeds or the alert is taken to chat — it can't be chosen here."""
    return await _out(await alert_service.close(user.id, alert_id, body.reason, body.note))


@router.post("/{alert_id}/act", response_model=AlertOut, deprecated=True)
async def act_on_alert(
    alert_id: str,
    body: AlertResolve | None = None,
    user: UserDoc = Depends(get_current_user),
):
    """Deprecated — use /close with `done_by_me`."""
    return await _out(await alert_service.act(user.id, alert_id, body.note if body else None))


@router.post("/{alert_id}/dismiss", response_model=AlertOut, deprecated=True)
async def dismiss_alert(
    alert_id: str,
    body: AlertResolve | None = None,
    user: UserDoc = Depends(get_current_user),
):
    """Deprecated — use /close with `not_useful`."""
    return await _out(
        await alert_service.dismiss(user.id, alert_id, body.note if body else None)
    )


@router.post("/{alert_id}/actions/{index}/run", response_model=AlertOut)
async def run_alert_action(
    alert_id: str,
    index: Annotated[int, Path(ge=0)],
    body: AlertActionRun | None = None,
    user: UserDoc = Depends(get_current_user),
):
    """Tap one of the alert's suggested actions. Starts an agent on it and returns the alert with
    the run in flight (`runs[-1].state == "running"`); poll the alert or wait for the push.
    Success closes the alert as `done_by_gaca`; failure leaves it open with the error on the run.
    409 if another action is already running or the alert is closed."""
    b = body or AlertActionRun()
    return await _out(
        await alert_service.run_action(
            user, alert_id, instruction=b.instruction, suggested_action_index=index, crew=b.crew
        )
    )


@router.post("/{alert_id}/take-to-chat", response_model=TakeToChatOut)
async def take_alert_to_chat(
    alert_id: str,
    body: AlertTakeToChat | None = None,
    user: UserDoc = Depends(get_current_user),
):
    """"Talk to Hermes about this." Opens the alert's chat — which always carries the alert as
    context, so nothing is copied into the message box — and closes the alert as `done_by_gaca`.
    `draft` is only the picked action's one-line prompt, if one was picked."""
    b = body or AlertTakeToChat()
    alert, session_id, draft = await alert_service.take_to_chat(
        user, alert_id, suggested_action_index=b.suggested_action_index, crew=b.crew
    )
    return TakeToChatOut(alert=await _out(alert), session_id=session_id, draft=draft)


@router.post("/{alert_id}/delegate", response_model=AlertOut, deprecated=True)
async def delegate_alert(
    alert_id: str,
    body: AlertDelegate | None = None,
    user: UserDoc = Depends(get_current_user),
):
    """Deprecated — use /actions/{index}/run, or this for a free-text instruction. Runs an agent
    on the alert the same way."""
    b = body or AlertDelegate()
    return await _out(
        await alert_service.run_action(
            user,
            alert_id,
            instruction=b.instruction,
            suggested_action_index=b.suggested_action_index,
            crew=b.crew,
        )
    )
