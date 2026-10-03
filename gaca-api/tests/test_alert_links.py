"""Alert links: validation, labeling, re-post semantics, and what Hermes is told."""
import asyncio

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from gaca_api.config import settings
from gaca_api.db import client
from gaca_api.main import app
from gaca_api.models.user import CrewMember, UserDoc
from gaca_api.schemas.alert import AlertCreate, AlertLinkIn, AlertOut
from gaca_api.services import alerts as alert_service
from gaca_api.utils.auth import get_current_user


def _payload(**overrides) -> AlertCreate:
    return AlertCreate(**{"kind": "calendar.reminder", "title": "Coming up: Soccer", **overrides})


# ── validation ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("url", ["https://calendar.example.com/#week-1", "http://example.com/x?y=1", " https://example.com/a "])
def test_web_urls_are_accepted(url):
    assert AlertLinkIn(url=url).url == url.strip()


@pytest.mark.parametrize("url", ["javascript:alert(1)", "file:///etc/passwd", "ftp://example.com", "//example.com",
                                 "example.com", "https://", "http:// spaces.example", "intent://x#Intent;end", ""])
def test_anything_that_is_not_a_web_address_is_refused(url):
    with pytest.raises(ValidationError):
        AlertLinkIn(url=url)


def test_labels_are_tidied_and_capped():
    assert AlertLinkIn(url="https://a.example", label="  Open   the\ncalendar ").label == "Open the calendar"
    assert AlertLinkIn(url="https://a.example", label="   ").label is None
    with pytest.raises(ValidationError):
        AlertLinkIn(url="https://a.example", label="x" * 81)


def test_at_most_ten_links_and_none_by_default():
    assert _payload().links == []
    ten = [{"url": f"https://a.example/{i}"} for i in range(10)]
    assert len(_payload(links=ten).links) == 10
    with pytest.raises(ValidationError):
        _payload(links=ten + [{"url": "https://a.example/11"}])


# ── what clients get back ────────────────────────────────────────────────────
def test_every_link_comes_back_labeled():
    from gaca_api.models.alert import AlertDoc
    doc = AlertDoc(user_id="u", kind="k", title="t", links=[
        {"url": "https://www.mail.google.com/mail/u/0/#all/abc"},                  # unlabeled -> its site
        {"url": "https://calendar.example.com/", "label": "Open family calendar"},  # labeled -> kept
    ])
    out = AlertOut(**doc.model_dump(), effective_status="new")
    assert [(l.label, l.url) for l in out.links] == [
        ("mail.google.com", "https://www.mail.google.com/mail/u/0/#all/abc"),
        ("Open family calendar", "https://calendar.example.com/"),
    ]


# ── re-posting an alert is a full restatement ────────────────────────────────
def _run(factory, tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def main():
        await client.connect()
        try:
            return await factory()
        finally:
            await client.close()

    return asyncio.run(main())


def test_links_are_stored_and_a_repost_replaces_them(tmp_path):
    async def go():
        first, created = await alert_service.create("u1", _payload(
            dedupe_key="k", links=[{"url": "https://a.example/1", "label": "One"}]))
        second, created_again = await alert_service.create("u1", _payload(
            dedupe_key="k", links=[{"url": "https://a.example/2"}]))
        third, _ = await alert_service.create("u1", _payload(dedupe_key="k"))  # a restatement with no links
        return first, created, second, created_again, third

    first, created, second, created_again, third = _run(go, tmp_path)
    assert created and not created_again and first.id == second.id == third.id
    assert [l.url for l in first.links] == ["https://a.example/1"]
    assert [l.url for l in second.links] == ["https://a.example/2"]
    assert third.links == []  # same rule as suggested actions: a stale link must not outlive its alert's facts


# ── what Hermes sees when the alert goes to a chat ───────────────────────────
def _user() -> UserDoc:
    return UserDoc(id="u1", email="u@example.com", display_name="T", agent_host="http://box",
                   crew=[CrewMember(name="default", port=8642, api_key="k")], default_crew="default")


def test_taking_an_alert_to_chat_includes_its_links_in_the_draft(tmp_path):
    async def go():
        with_links, _ = await alert_service.create("u1", _payload(
            title="Soccer at 5", body="Riverside Park", links=[
                {"url": "https://calendar.example.com/#week-1", "label": "Open family calendar"},
                {"url": "https://www.example.org/field-map"}]))
        plain, _ = await alert_service.create("u1", _payload(title="No links here"))
        action, _ = await alert_service.create("u1", _payload(
            title="x", links=[{"url": "https://a.example/z"}],
            suggested_actions=[{"label": "Do it", "prompt": "Do the thing"}]))
        return (
            (await alert_service.take_to_chat(_user(), with_links.id))[2],
            (await alert_service.take_to_chat(_user(), plain.id))[2],
            (await alert_service.take_to_chat(_user(), action.id, suggested_action_index=0))[2],
        )

    linked, plain, chosen = _run(go, tmp_path)
    assert linked == ("Soccer at 5\n\nRiverside Park\n\nLinks:\n"
                      "- Open family calendar: https://calendar.example.com/#week-1\n"
                      "- https://www.example.org/field-map: https://www.example.org/field-map")
    assert plain == "No links here"        # unchanged when there are none
    assert chosen == "Do the thing"        # a chosen suggestion's prompt stands on its own


# ── over HTTP ────────────────────────────────────────────────────────────────
@pytest.fixture
def http(tmp_path):
    settings.db_path = str(tmp_path / "t.db")
    app.dependency_overrides[get_current_user] = lambda: _user()
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_post_get_and_validation_over_http(http):
    body = {"kind": "calendar.reminder", "title": "Coming up: Soccer", "dedupe_key": "r1",
            "links": [{"url": "https://calendar.example.com/#week-1", "label": "Open family calendar"},
                      {"url": "https://www.example.org/map"}]}
    posted = http.post("/api/alerts", json=body)
    assert posted.status_code == 201
    assert [(l["label"], l["url"]) for l in posted.json()["links"]] == [
        ("Open family calendar", "https://calendar.example.com/#week-1"),
        ("example.org", "https://www.example.org/map"),
    ]
    listed = http.get("/api/alerts").json()["alerts"]
    assert listed[0]["links"][0]["label"] == "Open family calendar"
    assert http.post("/api/alerts", json={**body, "dedupe_key": "r2", "links": [{"url": "javascript:alert(1)"}]}).status_code == 422
    assert http.post("/api/alerts", json={**body, "dedupe_key": "r3", "links": [{"url": "https://x.example"}] * 11}).status_code == 422
    assert http.post("/api/alerts", json={"kind": "k", "title": "no links at all"}).json()["links"] == []


def test_a_posted_agent_name_comes_back_as_the_alerts_crew_over_http(http):
    posted = http.post("/api/alerts", json={"kind": "k", "title": "from a skill", "agent": "toolshed"})
    assert posted.status_code == 201 and posted.json()["crew"] == "toolshed"
    assert http.get("/api/alerts").json()["alerts"][0]["crew"] == "toolshed"
    assert http.post("/api/alerts", json={"kind": "k", "title": "explicit", "crew": "trading", "agent": "x"}).json()["crew"] == "trading"
