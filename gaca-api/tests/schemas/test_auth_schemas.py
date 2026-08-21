"""The agent-token request bounds are a contract, not a suggestion.

A token minted for an autonomous agent can't be revoked individually — rotating
GACA_JWT_SECRET is the only break-glass, and it logs everyone out. The 1..365
ceiling is what stops an unbounded-lifetime token being minted by accident.
"""
import pytest
from pydantic import ValidationError

from gaca_api.schemas.auth import AgentTokenRequest


def test_default_expiry_is_thirty_days():
    assert AgentTokenRequest().expires_in_days == 30


def test_bounds_are_inclusive():
    assert AgentTokenRequest(expires_in_days=1).expires_in_days == 1
    assert AgentTokenRequest(expires_in_days=365).expires_in_days == 365


@pytest.mark.parametrize("bad", [0, -1, 366, 10_000])
def test_out_of_range_expiry_rejected(bad):
    with pytest.raises(ValidationError):
        AgentTokenRequest(expires_in_days=bad)
