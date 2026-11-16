"""What an agent is and isn't allowed to post.

`severity` is a closed set because clients switch on it; `kind` is deliberately
open because agents invent new ones. Locking that pair down the wrong way round
is the easy mistake, so both directions are pinned here.
"""
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from gaca_api.schemas.alert import AlertClose, AlertCreate, AlertResolve


def _payload(**overrides) -> dict:
    return {"kind": "disk.low", "title": "Disk is 91% full", **overrides}


def test_severity_defaults_to_info():
    assert AlertCreate(**_payload()).severity == "info"


@pytest.mark.parametrize("severity", ["info", "warn", "urgent"])
def test_known_severities_accepted(severity):
    assert AlertCreate(**_payload(severity=severity)).severity == severity


@pytest.mark.parametrize("severity", ["critical", "URGENT", "", "high"])
def test_unknown_severity_rejected(severity):
    with pytest.raises(ValidationError):
        AlertCreate(**_payload(severity=severity))


def test_crew_defaults_to_none():
    assert AlertCreate(**_payload()).crew is None


def test_crew_accepts_an_arbitrary_profile_name():
    assert AlertCreate(**_payload(crew="home-alerts")).crew == "home-alerts"


def test_arbitrary_kind_accepted():
    assert AlertCreate(**_payload(kind="a.kind.invented.today")).kind


@pytest.mark.parametrize("missing", ["kind", "title"])
def test_kind_and_title_are_required(missing):
    payload = _payload()
    del payload[missing]
    with pytest.raises(ValidationError):
        AlertCreate(**payload)


def test_empty_kind_or_title_rejected():
    with pytest.raises(ValidationError):
        AlertCreate(**_payload(kind=""))
    with pytest.raises(ValidationError):
        AlertCreate(**_payload(title=""))


def test_suggested_actions_need_both_label_and_prompt():
    ok = AlertCreate(
        **_payload(suggested_actions=[{"label": "Clean logs", "prompt": "Free space…"}])
    )
    assert ok.suggested_actions[0].prompt == "Free space…"

    with pytest.raises(ValidationError):
        AlertCreate(**_payload(suggested_actions=[{"label": "Clean logs"}]))


def test_data_accepts_arbitrary_json():
    payload = AlertCreate(**_payload(data={"mount": "/", "used_pct": 91, "ok": False}))
    assert payload.data["used_pct"] == 91


# ── closing ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("reason", ["done_by_me", "noted", "not_useful"])
def test_close_accepts_the_reasons_a_person_can_give(reason):
    assert AlertClose(reason=reason).reason == reason


@pytest.mark.parametrize("reason", ["done_by_gaca", "new", "dismissed", "snoozed", ""])
def test_close_rejects_everything_else(reason):
    """done_by_gaca is the system's to set; the old words are gone."""
    with pytest.raises(ValidationError):
        AlertClose(reason=reason)


def test_a_suggested_action_may_carry_a_kind_and_params():
    payload = AlertCreate(**_payload(suggested_actions=[
        {"label": "Add to calendar", "prompt": "Add it", "kind": "calendar.add", "params": {"title": "Dentist"}},
        {"label": "Plain", "prompt": "Do it"},
    ]))
    first, second = payload.suggested_actions
    assert (first.kind, first.params) == ("calendar.add", {"title": "Dentist"})
    assert (second.kind, second.params) == (None, {})


def test_resolve_note_is_optional():
    assert AlertResolve().note is None
    assert AlertResolve(note="handled").note == "handled"
