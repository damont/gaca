"""The weekly efficiency alert: once a week, on the chosen day, an action per suggestion, and next
week a word on what the change did (with an undo if it didn't help)."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from gaca_api.config import settings
from gaca_api.db import alerts as alerts_db
from gaca_api.db import client
from gaca_api.models.alert import AlertRun
from gaca_api.models.user import CrewMember
from gaca_api.schemas.usage import UsageCost, UsageFinding, UsageLine, UsageReport
from gaca_api.services import alerts as alerts_service
from gaca_api.services import efficiency_watch as watch
from gaca_api.services import usage as usage_service
from tests.conftest import make_owner

MONDAY_9 = datetime(2026, 11, 30, 14, 0, tzinfo=timezone.utc)  # 9:00 in New York


def moves(total=50_000_000, previous=None):
    return UsageLine(crew="trading", kind="automation", automation_id="c989484fab1b", name="stock-notebook-price-moves",
                     runs=120, silent_runs=109, calls=750, prompt_tokens=total - 300_000, cached_tokens=total // 2,
                     output_tokens=300_000, total_tokens=total, share=0.4, previous_total_tokens=previous)


def report(lines=None, findings=None):
    lines = lines if lines is not None else [moves()]
    if findings is None:
        findings = usage_service.findings(lines, 7)
    total = sum(line.total_tokens for line in lines)
    return UsageReport(since=MONDAY_9 - timedelta(days=7), until=MONDAY_9, days=7, total_tokens=total,
                       previous_total_tokens=total * 2, lines=lines, findings=findings,
                       cost=UsageCost(label="Cheap", input_per_million=0.1, cached_per_million=0.01,
                                      output_per_million=0.5, usd=3.0, usd_per_month=12.9))


@pytest.fixture
def world(tmp_path, monkeypatch):
    settings.db_path = str(tmp_path / "t.db")
    monkeypatch.setattr(settings, "efficiency_weekday", 0)
    monkeypatch.setattr(settings, "efficiency_hour", 8)
    state = {"report": report(), "calls": 0}

    async def fake_report(user, days=7, until=None):
        state["calls"] += 1
        return state["report"]

    async def no_push(*a, **k):
        return None

    monkeypatch.setattr(usage_service, "report", fake_report)
    monkeypatch.setattr(alerts_service.push_service, "notify", no_push)
    return state


def run_in_db(coro_fn):
    async def go():
        await client.connect()
        try:
            owner = await make_owner()
            owner = owner.model_copy(update={
                "crew": [CrewMember(name="default", port=1, api_key="k", color="blue"),
                         CrewMember(name="trading", port=1, path="/p/trading", api_key="k2", color="green")],
                "heartbeat": owner.heartbeat.model_copy(update={"timezone": "America/New_York"})})
            return await coro_fn(owner)
        finally:
            await client.close()
    return asyncio.run(go())


def test_the_review_posts_one_alert_on_its_day_with_a_button_per_suggestion(world):
    async def go(owner):
        early = await watch.review(owner, MONDAY_9 - timedelta(hours=2))  # 7:00 local, before the hour
        posted = await watch.review(owner, MONDAY_9)
        again = await watch.review(owner, MONDAY_9 + timedelta(hours=1))
        return early, posted, again, await alerts_db.find_by_user(owner.id)

    early, posted, again, alerts = run_in_db(go)
    assert early is None and again is None and len(alerts) == 1
    assert world["calls"] == 1  # the numbers were read once for the week
    alert = posted
    assert alert.kind == "efficiency.weekly" and alert.crew == "default" and alert.dedupe_key == "efficiency:2026-W49"
    assert "Improve your efficiency" in alert.title
    assert "stock-notebook-price-moves (trading) 40%" in alert.body and "$13 a month" in alert.body
    [action] = alert.suggested_actions
    assert action.kind == "efficiency.prerun" and action.params["crew"] == "trading"
    assert "gaca-efficiency" in action.prompt and "c989484fab1b" in action.prompt and 'profile "trading"' in action.prompt
    assert "wakeAgent" in action.prompt and action.params["baseline_tokens"] == 50_000_000


def test_a_missed_day_is_caught_up_later_that_week(world):
    alert = run_in_db(lambda owner: watch.review(owner, MONDAY_9 + timedelta(days=2)))
    assert alert is not None and alert.dedupe_key == "efficiency:2026-W49"


def test_nothing_worth_saying_posts_nothing_and_is_not_rechecked_hourly(world):
    world["report"] = report(lines=[moves().model_copy(update={"share": 0.01, "silent_runs": 0})])

    async def go(owner):
        first = await watch.review(owner, MONDAY_9)
        second = await watch.review(owner, MONDAY_9 + timedelta(hours=1))
        return first, second, await alerts_db.find_by_user(owner.id)

    first, second, alerts = run_in_db(go)
    assert first is None and second is None and alerts == [] and world["calls"] == 1


def test_the_heartbeat_without_its_prerun_is_an_operator_note_not_a_button(world):
    beat = UsageLine(crew="default", kind="automation", automation_id="c85917c3a264", name="gaca-heartbeat", runs=300,
                     silent_runs=140, calls=2400, prompt_tokens=50_000_000, cached_tokens=40_000_000,
                     output_tokens=100_000, total_tokens=50_100_000, share=0.5)
    world["report"] = report(lines=[beat])
    alert = run_in_db(lambda owner: watch.review(owner, MONDAY_9))
    assert alert.suggested_actions == [] and "gaca-api hermes connect" in alert.body


def test_next_week_says_what_the_change_did_and_offers_undo_only_if_it_did_not_help(world):
    async def go(owner):
        first = await watch.review(owner, MONDAY_9)
        run = AlertRun(action_index=0, label="Add a pre-run check", state="done")
        await alerts_db.replace(first.model_copy(update={"runs": [run], "status": "done_by_gaca"}))
        world["report"] = report(lines=[moves(total=45_000_000)], findings=[])  # barely moved
        return await watch.review(owner, MONDAY_9 + timedelta(days=7))

    second = run_in_db(go)
    assert "50.0M → 45.0M tokens (-10%)" in second.body
    [undo] = second.suggested_actions
    assert undo.kind == "efficiency.undo" and "c989484fab1b" in undo.prompt and "undo" in undo.prompt


def test_a_change_that_helped_is_reported_without_an_undo(world):
    async def go(owner):
        first = await watch.review(owner, MONDAY_9)
        run = AlertRun(action_index=0, label="Add a pre-run check", state="done")
        await alerts_db.replace(first.model_copy(update={"runs": [run], "status": "done_by_gaca"}))
        world["report"] = report(lines=[moves(total=5_000_000)], findings=[])
        return await watch.review(owner, MONDAY_9 + timedelta(days=7))

    second = run_in_db(go)
    assert "(-90%)" in second.body and second.suggested_actions == []


def test_nothing_readable_is_retried_next_hour_not_marked_done(world):
    world["report"] = UsageReport(since=MONDAY_9, until=MONDAY_9, days=7, unavailable=["default", "trading"])

    async def go(owner):
        await watch.review(owner, MONDAY_9)
        world["report"] = report()
        return await watch.review(owner, MONDAY_9 + timedelta(hours=1))

    assert run_in_db(go) is not None


def test_review_now_runs_regardless_of_the_day(world):
    alert = run_in_db(lambda owner: watch.review(owner, MONDAY_9 + timedelta(days=-3), force=True))
    assert alert is not None


def test_findings_go_in_order_and_unknown_rules_are_skipped(world):
    odd = UsageFinding(rule="something-new", crew="default", name="x", title="t", detail="d")
    world["report"] = report(findings=[*usage_service.findings([moves()], 7), odd])
    alert = run_in_db(lambda owner: watch.review(owner, MONDAY_9))
    assert [a.kind for a in alert.suggested_actions] == ["efficiency.prerun"]
