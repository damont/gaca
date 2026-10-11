"""The agent-token request bounds are a contract, not a suggestion.

A token minted for an autonomous agent can't be revoked individually — rotating
GACA_JWT_SECRET is the only break-glass, and it logs everyone out. The 1..365
ceiling is what stops an unbounded-lifetime token being minted by accident.
"""
import pytest
from pydantic import ValidationError

from gaca_api.schemas.auth import (
    AgentConnect,
    AgentHostUpdate,
    CrewAccess,
    CrewMemberCreate,
    CrewMemberUpdate,
)


# ── AgentConnect / AgentHostUpdate ────────────────────────────────────

def test_agent_connect_defaults_the_first_crew_member_name_to_default():
    connect = AgentConnect(host="http://david-claw", port=8642, api_key="k")
    assert connect.name == "default"


def test_agent_connect_requires_a_non_empty_host_and_key():
    with pytest.raises(ValidationError):
        AgentConnect(host="", port=8642, api_key="k")
    with pytest.raises(ValidationError):
        AgentConnect(host="http://david-claw", port=8642, api_key="")


def test_agent_host_update_requires_a_non_empty_value():
    assert AgentHostUpdate(host="http://david-claw").host == "http://david-claw"
    with pytest.raises(ValidationError):
        AgentHostUpdate(host="")


def test_crew_access_defaults_port_to_22():
    conn = CrewAccess(host="david-claw.lan", username="david", private_key="k")
    assert conn.port == 22


def test_crew_access_requires_host_username_and_key():
    with pytest.raises(ValidationError):
        CrewAccess(host="", username="david", private_key="k")
    with pytest.raises(ValidationError):
        CrewAccess(host="david-claw.lan", username="", private_key="k")
    with pytest.raises(ValidationError):
        CrewAccess(host="david-claw.lan", username="david", private_key="")


# ── CrewMemberCreate / CrewMemberUpdate ───────────────────────────────


@pytest.mark.parametrize("port", [1, 8643, 65535])
def test_crew_member_create_accepts_valid_ports(port):
    assert CrewMemberCreate(name="toolshed", port=port, api_key="k").port == port


@pytest.mark.parametrize("port", [0, -1, 65536, 100_000])
def test_crew_member_create_rejects_out_of_range_ports(port):
    with pytest.raises(ValidationError):
        CrewMemberCreate(name="toolshed", port=port, api_key="k")


def test_crew_member_create_requires_a_non_empty_name_and_key():
    with pytest.raises(ValidationError):
        CrewMemberCreate(name="", port=8643, api_key="k")
    with pytest.raises(ValidationError):
        CrewMemberCreate(name="toolshed", port=8643, api_key="")


def test_crew_member_create_color_defaults_to_none_for_auto_assignment():
    assert CrewMemberCreate(name="toolshed", port=8643, api_key="k").color is None


def test_crew_member_create_rejects_a_non_hex_color():
    with pytest.raises(ValidationError):
        CrewMemberCreate(name="toolshed", port=8643, api_key="k", color="blue")


def test_crew_member_create_allows_omitting_port_and_key_together():
    """Omitting both means "gaca-api, create this one for me" — a real,
    valid request, not a validation failure."""
    created = CrewMemberCreate(name="toolshed")
    assert created.port is None
    assert created.api_key is None


def test_crew_member_create_rejects_only_one_of_port_or_key():
    with pytest.raises(ValidationError):
        CrewMemberCreate(name="toolshed", port=8643)
    with pytest.raises(ValidationError):
        CrewMemberCreate(name="toolshed", api_key="k")


def test_crew_member_create_access_defaults_to_none():
    """No separate setup call needed just to check whether it's there —
    omitting it means "use whatever gaca-api already has, if anything.\""""
    assert CrewMemberCreate(name="toolshed").access is None


def test_crew_member_create_accepts_inline_access():
    created = CrewMemberCreate(
        name="toolshed",
        access={"host": "david-claw.lan", "username": "david", "private_key": "k"},
    )
    assert created.access.host == "david-claw.lan"
    assert created.access.port == 22


def test_crew_member_update_has_no_name_field():
    """Renaming isn't supported — the name is the path, not the body; this
    just re-submits port and key together, same no-partial-update rule."""
    updated = CrewMemberUpdate(port=8643, api_key="k")
    assert not hasattr(updated, "name")
