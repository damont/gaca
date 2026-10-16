"""What an agent is and isn't allowed to post.

`severity` is a closed set because clients switch on it; `kind` is deliberately
open because agents invent new ones. Locking that pair down the wrong way round
is the easy mistake, so both directions are pinned here.
"""
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from gaca_api.schemas.alert import AlertCreate, AlertResolve, AlertSnooze


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


# ── snooze ──────────────────────────────────────────────────────────

def test_snooze_accepts_a_future_time():
    until = datetime.now(timezone.utc) + timedelta(hours=1)
    assert AlertSnooze(until=until).until == until


def test_snooze_normalizes_a_naive_time_to_utc():
    naive = (datetime.now(timezone.utc) + timedelta(hours=1)).replace(tzinfo=None)
    assert AlertSnooze(until=naive).until.tzinfo is not None


@pytest.mark.parametrize("delta", [timedelta(seconds=-1), timedelta(days=-1)])
def test_snooze_into_the_past_rejected(delta):
    """It would be a no-op that looks like it worked.

    Effective status derives from this field, so a past `until` means the alert
    reappears as `new` on the very next read.
    """
    with pytest.raises(ValidationError):
        AlertSnooze(until=datetime.now(timezone.utc) + delta)


def test_resolve_note_is_optional():
    assert AlertResolve().note is None
    assert AlertResolve(note="handled").note == "handled"
