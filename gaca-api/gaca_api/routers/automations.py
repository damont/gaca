"""HTTP layer for automations — the scheduled work a user's crew members do.

Thin: validate the request, call the service, return its schema. Read live from each
member's engine every time (see schemas/automation.py for the neutral shape). Creating and
editing automations is done by talking to the crew member in chat.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status

from ..models.user import UserDoc
from ..schemas.automation import Automation, AutomationList, AutomationRun
from ..services import automations as service
from ..utils.auth import get_current_user

router = APIRouter(tags=["automations"])


@router.get("/automations", response_model=AutomationList)
async def list_automations(
    crew: str | None = Query(None, description="Only this crew member's."),
    include_done: bool = Query(False, description="Also finished one-time automations."),
    user: UserDoc = Depends(get_current_user),
):
    """Every crew member's automations, soonest next run first, each with its last runs
    (`recent`, oldest first). A crew member whose engine can't be read is listed in
    `unavailable`; the rest still come back."""
    return await service.list_all(user, crew, include_done)


@router.get("/crew/{name}/automations/{job_id}/runs", response_model=list[AutomationRun])
async def automation_runs(
    name: str,
    job_id: str,
    limit: int = Query(20, ge=1, le=100),
    user: UserDoc = Depends(get_current_user),
):
    """One automation's run history, newest first: when, how long, whether it worked, and the
    end of what it said (or why it failed). `502` if the history can't be read."""
    return await service.runs(user, name, job_id, limit)


@router.post("/crew/{name}/automations/{job_id}/run", response_model=Automation, status_code=status.HTTP_202_ACCEPTED)
async def run_automation(name: str, job_id: str, user: UserDoc = Depends(get_current_user)):
    """Run it now. A request, not an instant start: the engine begins on its next tick (about a
    minute), so poll the runs or the list. `422` if the engine refuses (a finished one-time job)."""
    return await service.control(user, name, job_id, "run")


@router.post("/crew/{name}/automations/{job_id}/pause", response_model=Automation)
async def pause_automation(name: str, job_id: str, user: UserDoc = Depends(get_current_user)):
    """Stop it running on its schedule until resumed."""
    return await service.control(user, name, job_id, "pause")


@router.post("/crew/{name}/automations/{job_id}/resume", response_model=Automation)
async def resume_automation(name: str, job_id: str, user: UserDoc = Depends(get_current_user)):
    """Put a paused automation back on its schedule."""
    return await service.control(user, name, job_id, "resume")
