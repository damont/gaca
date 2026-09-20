"""UserDoc's backward-compat shim across two shape changes (2026-09).

Every row already in gaca.db from the 2026-08-22 multi-tenant retrofit was
written under `agent_host`/`agents`/`default_agent`. A same-day 2026-09-20
pass briefly renamed `agent_host` to `crew_host` before reverting it (see
build-plan.md §11/§12) — `agents`/`default_agent` -> `crew`/`default_crew`
stuck, though. Without the migration validator, loading either older shape
back would silently read as an unconfigured account — these tests are what
make sure that never regresses.
"""
import json

from gaca_api.models.user import CREW_COLOR_PALETTE, UserDoc

_BASE = dict(
    id="u1", email="david@example.com", display_name="David",
    created_at="2026-01-01T00:00:00Z",
)


def test_agent_host_needs_no_migration_the_name_never_actually_changed():
    """`agent_host` is both the 2026-08-22 name and the current one — the
    brief `crew_host` rename reverted the same day, so a row from either end
    of that timeline reads directly, no shim required for this key."""
    user = UserDoc.model_validate({**_BASE, "agent_host": "http://david-claw"})
    assert user.agent_host == "http://david-claw"


def test_legacy_crew_host_maps_onto_agent_host():
    """The one shape that DOES need a shim: a row saved during the brief
    2026-09-20 `crew_host` window, if one ever existed."""
    user = UserDoc.model_validate({**_BASE, "crew_host": "http://david-claw"})
    assert user.agent_host == "http://david-claw"


def test_legacy_default_agent_maps_onto_default_crew():
    user = UserDoc.model_validate({**_BASE, "default_agent": "toolshed"})
    assert user.default_crew == "toolshed"


def test_legacy_agents_list_maps_onto_crew_with_backfilled_colors():
    user = UserDoc.model_validate({
        **_BASE,
        "agents": [
            {"name": "toolshed", "port": 8643, "api_key": "k1"},
            {"name": "trading", "port": 8644, "api_key": "k2"},
        ],
    })
    assert [c.name for c in user.crew] == ["toolshed", "trading"]
    assert user.crew[0].color == CREW_COLOR_PALETTE[0]
    assert user.crew[1].color == CREW_COLOR_PALETTE[1]
    assert user.crew[0].color != user.crew[1].color


def test_new_keys_win_when_both_are_somehow_present():
    """Belt-and-braces: a row already saved under the current key is never
    clobbered by a stray old one."""
    user = UserDoc.model_validate({
        **_BASE, "crew_host": "http://old", "agent_host": "http://new",
    })
    assert user.agent_host == "http://new"


def test_round_trips_through_json_like_the_real_store_does():
    """model_validate_json is what db/users.py actually calls — prove the
    migration survives that path too, not just a plain dict."""
    raw = json.dumps({
        **_BASE,
        "agent_host": "http://david-claw",
        "agents": [{"name": "toolshed", "port": 8643, "api_key": "k1"}],
        "default_agent": "toolshed",
    })
    user = UserDoc.model_validate_json(raw)
    assert user.agent_host == "http://david-claw"
    assert user.default_crew == "toolshed"
    assert user.crew[0].name == "toolshed"
