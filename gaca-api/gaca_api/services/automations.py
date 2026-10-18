"""A user's automations: scheduled work their crew members do, read from each member's engine.

No FastAPI, no HTTP. Joins the crew GACA knows about with what each engine reports, in the
neutral terms of schemas/automation.py. Never cached: every call asks the engine.
"""
from __future__ import annotations

import asyncio
import re

from ..agent.base import AutomationNotFound, AutomationRejected, EngineUnavailable, ProfileReadError
from ..models.user import CrewMember, UserDoc
from ..provisioning.select import provisioner_for
from ..schemas.automation import Automation, AutomationList, AutomationRun
from . import auth as auth_service
from .exceptions import InvalidRequest, NotFound

_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_READ_TIMEOUT = 15.0


def _member(user: UserDoc, name: str) -> CrewMember:
    member = auth_service.find_crew_member(user, name)
    if member is None:
        raise NotFound("crew member", name)
    return member


def _job_id(job_id: str) -> str:
    if not _ID.fullmatch(job_id):
        raise NotFound("automation", job_id)
    return job_id


async def _for_member(user: UserDoc, member: CrewMember, include_done: bool) -> list[Automation]:
    backend = auth_service.backend_for_crew(user, member)
    found = await asyncio.wait_for(
        backend.list_automations(name=member.name, provisioner=provisioner_for(user), include_done=include_done),
        timeout=_READ_TIMEOUT,
    )
    for automation in found:
        automation.crew = member.name
    return found


async def list_all(user: UserDoc, crew: str | None = None, include_done: bool = False) -> AutomationList:
    """Every crew member's automations (or just `crew`'s), soonest next run first. A member whose
    engine can't be read is named in `unavailable` and the rest are still returned."""
    members = [_member(user, crew)] if crew else list(user.crew)
    results = await asyncio.gather(
        *(_for_member(user, m, include_done) for m in members), return_exceptions=True
    )
    out = AutomationList()
    for member, result in zip(members, results):
        if isinstance(result, (EngineUnavailable, asyncio.TimeoutError)):
            out.unavailable.append(member.name)
        elif isinstance(result, BaseException):
            raise result
        else:
            out.automations.extend(result)
    out.automations.sort(key=lambda a: (a.next_run is None, a.next_run.timestamp() if a.next_run else 0.0, a.name))
    return out


async def runs(user: UserDoc, name: str, job_id: str, limit: int) -> list[AutomationRun]:
    member = _member(user, name)
    provisioner = provisioner_for(user)
    if provisioner is None:
        raise EngineUnavailable("Run history needs gaca-api to have access to the agent's machine.")
    backend = auth_service.backend_for_crew(user, member)
    try:
        return await asyncio.wait_for(
            backend.automation_runs(name=member.name, job_id=_job_id(job_id), limit=limit, provisioner=provisioner),
            timeout=_READ_TIMEOUT,
        )
    except AutomationNotFound:
        raise NotFound("automation", job_id) from None
    except (ProfileReadError, asyncio.TimeoutError) as error:
        raise EngineUnavailable(str(error) or "Couldn't read the run history in time.") from error


async def run_now(user: UserDoc, name: str, job_id: str) -> Automation:
    """Ask for a run now, then return the automation as it now stands."""
    member = _member(user, name)
    backend = auth_service.backend_for_crew(user, member)
    try:
        await backend.run_automation(job_id=_job_id(job_id))
    except AutomationNotFound:
        raise NotFound("automation", job_id) from None
    except AutomationRejected as why:
        raise InvalidRequest(str(why)) from None
    for automation in await _for_member(user, member, include_done=True):
        if automation.id == job_id:
            return automation
    raise NotFound("automation", job_id)
