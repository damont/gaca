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
)
from ..services import alerts as alert_service
from ..utils.auth import get_current_user

router = APIRouter(prefix="/alerts", tags=["alerts"])


def _out(alert: AlertDoc) -> AlertOut:
    """Map a stored alert to the client shape, deriving effective_status."""
    return AlertOut(
        **alert.model_dump(),
        effective_status=alert_service.effective_status(alert),
    )


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
    return _out(alert)


@router.get("", response_model=AlertList)
async def list_alerts(
    user: UserDoc = Depends(get_current_user),
    # Repeatable: ?status=new&status=snoozed. Filtered on EFFECTIVE status, so
    # an elapsed snooze shows up under `new`.
    status: Annotated[list[str] | None, Query()] = None,
    severity: Annotated[list[str] | None, Query()] = None,
    agent: Annotated[list[str] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
):
    """The inbox: this user's alerts, newest first."""
    alerts = await alert_service.list_for_user(
        user.id, statuses=status, severities=severity, agents=agent, limit=limit
    )
    return AlertList(alerts=[_out(a) for a in alerts])


@router.get("/{alert_id}", response_model=AlertOut)
async def get_alert(alert_id: str, user: UserDoc = Depends(get_current_user)):
    return _out(await alert_service.get(user.id, alert_id))


@router.post("/{alert_id}/act", response_model=AlertOut)
async def act_on_alert(
    alert_id: str,
    body: AlertResolve | None = None,
    user: UserDoc = Depends(get_current_user),
):
    """"I handled it myself." Terminal."""
    return _out(await alert_service.act(user.id, alert_id, body.note if body else None))


@router.post("/{alert_id}/dismiss", response_model=AlertOut)
async def dismiss_alert(
    alert_id: str,
    body: AlertResolve | None = None,
    user: UserDoc = Depends(get_current_user),
):
    """"Ignore this." Terminal — though a re-fired dedupe_key resurfaces it."""
    return _out(
        await alert_service.dismiss(user.id, alert_id, body.note if body else None)
    )


@router.post("/{alert_id}/snooze", response_model=AlertOut)
async def snooze_alert(
    alert_id: str,
    body: AlertSnooze,
    user: UserDoc = Depends(get_current_user),
):
    """"Not now." Reads as `new` again once `until` passes."""
    return _out(await alert_service.snooze(user.id, alert_id, body.until))


@router.post("/{alert_id}/delegate", response_model=AlertOut)
async def delegate_alert(
    alert_id: str,
    body: AlertDelegate | None = None,
    user: UserDoc = Depends(get_current_user),
):
    """"Hand it to Hermes." Starts a real run; the alert settles once it ends."""
    b = body or AlertDelegate()
    return _out(
        await alert_service.delegate(
            user,
            alert_id,
            instruction=b.instruction,
            suggested_action_index=b.suggested_action_index,
            agent=b.agent,
        )
    )
