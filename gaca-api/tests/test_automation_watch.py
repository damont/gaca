"""Failed scheduled runs become alerts: once each, only recent ones, never resurrecting a dismissed one."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from gaca_api.agent.base import EngineUnavailable
from gaca_api.config import settings
from gaca_api.db import alerts as alerts_db
from gaca_api.db import client
from gaca_api.models.user import CrewMember, UserDoc
from gaca_api.schemas.automation import Automation, AutomationList, AutomationRun, RunMark
from gaca_api.services import alerts as alerts_service
from gaca_api.services import automation_watch as watch
from gaca_api.services import automations as automations_service
from tests.conftest import make_owner

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone(timedelta(hours=-4)))


def automation(job="aaaaaaaaaaaa", marks=(), crew="default", name="Email to calendar"):
    return Automation(id=job, name=name, crew=crew, schedule="Every day at 7:30 AM", status="failed",
                      recent=[RunMark(id=i, status=s, at=at) for i, s, at in marks])


def failed_at(minutes_ago, run_id="e9"):
    return (run_id, "failed", NOW - timedelta(minutes=minutes_ago))


@pytest.fixture
def world(tmp_path, monkeypatch):
    settings.db_path = str(tmp_path / "t.db")
    state = {"automations": [], "runs": [AutomationRun(id="e9", status="failed", error="Timed out waiting for the Gmail connector.")],
             "runs_error": None}

    async def list_all(user, crew=None, include_done=False):
        return AutomationList(automations=state["automations"])

    async def runs(user, name, job_id, limit):
        if state["runs_error"]:
            raise state["runs_error"]
        return state["runs"]

    monkeypatch.setattr(automations_service, "list_all", list_all)
    monkeypatch.setattr(automations_service, "runs", runs)
    monkeypatch.setattr(alerts_service.push_service, "notify", _no_push)
    return state


async def _no_push(*a, **k):
    return None


def run_in_db(coro_fn):
    async def go():
        await client.connect()
        try:
            owner = await make_owner()
            owner = owner.model_copy(update={"crew": [CrewMember(name="default", port=1, api_key="k", color="blue")]})
            return await coro_fn(owner)
        finally:
            await client.close()
    return asyncio.run(go())


def test_a_fresh_failure_becomes_one_alert_with_the_reason(world):
    world["automations"] = [automation(marks=[("e7", "ok", NOW - timedelta(days=1)), failed_at(30)])]

    async def go(owner):
        first = await watch.check(owner, NOW)
        second = await watch.check(owner, NOW + timedelta(minutes=1))
        return first, second, await alerts_db.find_by_user(owner.id)

    first, second, alerts = run_in_db(go)
    assert (first, second) == (1, 0)  # seen twice, announced once
    [alert] = alerts
    assert alert.title == "Email to calendar failed" and alert.severity == "warn" and alert.kind == "automation_failed"
    assert alert.crew == "default" and alert.dedupe_key == "automation:default:aaaaaaaaaaaa:e9"
    assert "Timed out waiting for the Gmail connector." in alert.body and "Every day at 7:30 AM".lower() in alert.body
    assert "Find out why" == alert.suggested_actions[0].label and "aaaaaaaaaaaa" in alert.suggested_actions[0].prompt


def test_old_failures_and_successes_and_unnamed_runs_are_ignored(world):
    world["automations"] = [
        automation("a" * 12, marks=[failed_at(60 * 24)]),                             # a day old
        automation("b" * 12, marks=[("e1", "failed", NOW - timedelta(hours=1)), ("e2", "ok", NOW)]),  # failed, then worked
        automation("c" * 12, marks=[("e3", "running", NOW)]),
        automation("d" * 12, marks=[(None, "failed", NOW)]),                           # no run id to dedupe on
        automation("e" * 12, marks=[("e4", "failed", datetime(2026, 10, 18, 11, 59))]),  # no timezone
        automation("f" * 12, marks=[]),
    ]
    assert run_in_db(lambda owner: watch.check(owner, NOW)) == 0


def test_a_dismissed_alert_stays_dismissed(world):
    world["automations"] = [automation(marks=[failed_at(10)])]

    async def go(owner):
        await watch.check(owner, NOW)
        [alert] = await alerts_db.find_by_user(owner.id)
        await alerts_service.dismiss(owner.id, alert.id)
        again = await watch.check(owner, NOW + timedelta(minutes=1))
        [after] = await alerts_db.find_by_user(owner.id)
        return again, after.status

    assert run_in_db(go) == (0, "dismissed")


def test_the_next_failure_is_a_new_alert(world):
    world["automations"] = [automation(marks=[failed_at(10, "e9")])]

    async def go(owner):
        await watch.check(owner, NOW)
        world["automations"] = [automation(marks=[failed_at(10, "e9"), failed_at(2, "e10")])]
        await watch.check(owner, NOW)
        return len(await alerts_db.find_by_user(owner.id))

    assert run_in_db(go) == 2  # e9 on the first pass, e10 on the second


def test_no_reason_available_still_alerts(world):
    world["automations"] = [automation(marks=[failed_at(5)])]
    world["runs_error"] = EngineUnavailable("box unreachable")

    async def go(owner):
        await watch.check(owner, NOW)
        return (await alerts_db.find_by_user(owner.id))[0]

    alert = run_in_db(go)
    assert "failed without saying why" in alert.body and "Error:" not in alert.suggested_actions[0].prompt


def test_the_loop_survives_a_bad_pass_and_stops_cleanly(world, monkeypatch):
    calls = []

    async def first():
        calls.append(1)
        raise RuntimeError("db hiccup") if len(calls) == 1 else asyncio.CancelledError()

    monkeypatch.setattr(watch.users_db, "first", first)
    monkeypatch.setattr(settings, "automation_watch_seconds", 0.01)
    monkeypatch.setattr(settings, "automation_alerts", True)

    async def go():
        task = watch.start()
        assert task is not None
        await asyncio.sleep(0.2)
        await watch.stop(task)
        return len(calls)

    assert asyncio.run(go()) >= 2  # the first pass failed and the loop carried on
    monkeypatch.setattr(settings, "automation_alerts", False)
    assert watch.start() is None
