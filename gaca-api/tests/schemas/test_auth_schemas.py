"""The agent-token request bounds are a contract, not a suggestion.

A token minted for an autonomous agent can't be revoked individually — rotating
GACA_JWT_SECRET is the only break-glass, and it logs everyone out. The 1..365
ceiling is what stops an unbounded-lifetime token being minted by accident.
"""
import pytest
from pydantic import ValidationError

from gaca_api.schemas.auth import AgentCreate, AgentHostUpdate, AgentTokenRequest, AgentUpdate


def test_default_expiry_is_thirty_days():
    assert AgentTokenRequest().expires_in_days == 30


def test_bounds_are_inclusive():
    assert AgentTokenRequest(expires_in_days=1).expires_in_days == 1
    assert AgentTokenRequest(expires_in_days=365).expires_in_days == 365


@pytest.mark.parametrize("bad", [0, -1, 366, 10_000])
def test_out_of_range_expiry_rejected(bad):
    with pytest.raises(ValidationError):
        AgentTokenRequest(expires_in_days=bad)


# ── AgentHostUpdate / AgentCreate / AgentUpdate ──────────────────────

def test_agent_host_requires_a_non_empty_value():
    assert AgentHostUpdate(host="http://david-claw").host == "http://david-claw"
    with pytest.raises(ValidationError):
        AgentHostUpdate(host="")


@pytest.mark.parametrize("port", [1, 8643, 65535])
def test_agent_create_accepts_valid_ports(port):
    assert AgentCreate(name="toolshed", port=port, api_key="k").port == port


@pytest.mark.parametrize("port", [0, -1, 65536, 100_000])
def test_agent_create_rejects_out_of_range_ports(port):
    with pytest.raises(ValidationError):
        AgentCreate(name="toolshed", port=port, api_key="k")


def test_agent_create_requires_a_non_empty_name_and_key():
    with pytest.raises(ValidationError):
        AgentCreate(name="", port=8643, api_key="k")
    with pytest.raises(ValidationError):
        AgentCreate(name="toolshed", port=8643, api_key="")


def test_agent_update_has_no_name_field():
    """Renaming isn't supported — the name is the path, not the body; this
    just re-submits port and key together, same no-partial-update rule."""
    updated = AgentUpdate(port=8643, api_key="k")
    assert not hasattr(updated, "name")
