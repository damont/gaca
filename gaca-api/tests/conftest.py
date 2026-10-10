"""Test-wide setup: the app refuses to start with the placeholder JWT secret unless GACA_DEV is
on, and the rate limiter keeps state between requests, so each test starts clean."""
import pytest

from gaca_api import security
from gaca_api.config import settings


@pytest.fixture(autouse=True)
def _dev_mode_and_fresh_limiter(monkeypatch):
    monkeypatch.setattr(settings, "dev", True)
    security.limiter.reset()
    yield
    security.limiter.reset()
