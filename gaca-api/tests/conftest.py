"""Test-wide setup: the rate limiter keeps state between requests, so each test starts clean.
Also `make_owner`, the way tests get the one owner record without going through pairing."""
import pytest

from gaca_api import security
from gaca_api.db import users as users_db
from gaca_api.models.user import UserDoc


@pytest.fixture(autouse=True)
def _fresh_limiter():
    security.limiter.reset()
    yield
    security.limiter.reset()


async def make_owner(name: str = "Owner", email: str = "o@example.com") -> UserDoc:
    """Insert an owner directly (service-level tests; HTTP tests pair a device instead)."""
    user = UserDoc(email=email.strip().lower(), display_name=name)
    await users_db.insert(user)
    return user
