"""Tests for the daily briefing service + request schemas."""
import asyncio
from datetime import date

import pytest
from pydantic import ValidationError

from gaca_api.config import settings
from gaca_api.db import client
from gaca_api.schemas.briefing import BriefingItem, SectionPut
from gaca_api.services import briefings as svc
from gaca_api.services.exceptions import NotFound

DAY = date(2026, 10, 1)


def _run(coro_fn, tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def main():
        await client.connect()
        try:
            return await coro_fn()
        finally:
            await client.close()

    return asyncio.run(main())


def _section(title="Calendar", order=0, items=("Soccer",)):
    return SectionPut(
        title=title, kind="calendar", display_order=order,
        items=[BriefingItem(title=t) for t in items],
    )


def test_empty_day_is_an_empty_briefing_not_an_error(tmp_path):
    out = _run(lambda: svc.get("u1", DAY), tmp_path)
    assert out.sections == [] and out.summary is None and out.updated_at is None


def test_sections_come_back_in_display_order_with_summary(tmp_path):
    async def go():
        await svc.put_section("u1", DAY, "news", _section("News", order=2))
        await svc.put_section("u1", DAY, "weather", _section("Weather", order=1))
        await svc.put_section("u1", DAY, "calendar", _section("Calendar", order=1))
        await svc.set_summary("u1", DAY, "  Busy day  ")
        return await svc.get("u1", DAY)

    out = _run(go, tmp_path)
    assert [s.key for s in out.sections] == ["calendar", "weather", "news"]  # ties by key
    assert out.summary == "Busy day"
    assert out.updated_at is not None


def test_putting_a_section_again_replaces_it(tmp_path):
    async def go():
        await svc.put_section("u1", DAY, "calendar", _section(items=("A", "B")))
        await svc.put_section("u1", DAY, "calendar", _section(items=("C",)))
        return await svc.get("u1", DAY)

    out = _run(go, tmp_path)
    assert len(out.sections) == 1
    assert [i.title for i in out.sections[0].items] == ["C"]


def test_briefings_are_scoped_per_user_and_per_day(tmp_path):
    async def go():
        await svc.put_section("u1", DAY, "calendar", _section())
        return (
            await svc.get("u2", DAY),
            await svc.get("u1", date(2026, 10, 2)),
        )

    other_user, other_day = _run(go, tmp_path)
    assert other_user.sections == [] and other_day.sections == []


def test_delete_section_and_missing_is_not_found(tmp_path):
    async def go():
        await svc.put_section("u1", DAY, "calendar", _section())
        await svc.delete_section("u1", DAY, "calendar")
        remaining = await svc.get("u1", DAY)
        with pytest.raises(NotFound):
            await svc.delete_section("u1", DAY, "calendar")
        return remaining

    assert _run(go, tmp_path).sections == []


def test_list_dates_newest_first_including_summary_only_days(tmp_path):
    async def go():
        await svc.put_section("u1", date(2026, 9, 29), "calendar", _section())
        await svc.set_summary("u1", date(2026, 10, 1), "hi")
        await svc.put_section("u1", date(2026, 10, 1), "news", _section())
        await svc.put_section("u2", date(2026, 12, 1), "news", _section())
        return await svc.list_dates("u1")

    assert _run(go, tmp_path) == [date(2026, 10, 1), date(2026, 9, 29)]


def test_schema_rejects_non_web_links_and_bad_kinds():
    with pytest.raises(ValidationError):
        BriefingItem(title="x", link="javascript:alert(1)")
    assert BriefingItem(title="x", link="https://example.com/e/1").link
    with pytest.raises(ValidationError):
        SectionPut(title="x", kind="astrology")
    with pytest.raises(ValidationError):
        BriefingItem(title="x", priority="urgent")
