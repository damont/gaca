"""AlertDoc's backward-compat shim for the agent→crew rename (2026-09).

An alert already stored under the old `agent` key must still report who
posted it after the rename, not read back as None.
"""
from gaca_api.models.alert import AlertDoc

_BASE = dict(user_id="u1", kind="disk.low", title="Disk is 91% full")


def test_legacy_agent_field_maps_onto_crew():
    alert = AlertDoc.model_validate({**_BASE, "agent": "toolshed"})
    assert alert.crew == "toolshed"


def test_new_field_wins_when_both_are_somehow_present():
    alert = AlertDoc.model_validate({**_BASE, "agent": "old", "crew": "new"})
    assert alert.crew == "new"


def test_no_legacy_key_still_defaults_to_none():
    alert = AlertDoc.model_validate(_BASE)
    assert alert.crew is None
