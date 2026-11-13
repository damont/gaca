"""Test-wide setup: the rate limiter keeps state between requests, so each test starts clean.
Also `make_owner`, the way tests get the one owner record without going through pairing."""
import pytest

from gaca_api import security
from gaca_api.db import users as users_db
from gaca_api.models.user import UserDoc


def _tree(root):
    import os

    skip = {".venv", "__pycache__", ".pytest_cache", ".git", "node_modules"}
    out = set()
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in skip]
        out.update(os.path.join(base, f) for f in files if not f.endswith(".pyc"))
    return out


@pytest.fixture(scope="session", autouse=True)
def _suite_leaves_the_repo_clean():
    """No test may write into the repo (or the cwd): a cloud key file, a database, anything. The cloud link
    writes its Fernet key next to the database, so a test that forgets to point db_path at tmp_path would
    leave a real key in the working tree (it happened once). Fails the run, naming the files."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    before = _tree(root)
    yield
    created = sorted(_tree(root) - before)
    assert not created, f"tests wrote into the repository: {created}"


@pytest.fixture(autouse=True)
def _cloud_key_from_the_environment(monkeypatch):
    """With no GACA_CLOUD_KEY the cloud link creates a key FILE next to the database. A test must never do
    that by accident (wherever db_path points), so every test starts with a throwaway env key; the tests
    of the key file itself set it to "" and point db_path at tmp_path."""
    from cryptography.fernet import Fernet

    from gaca_api.config import settings

    monkeypatch.setattr(settings, "cloud_key", Fernet.generate_key().decode())


@pytest.fixture(autouse=True)
def _no_background_watchers(monkeypatch):
    """The automation watcher is a background task of the running app; tests call it directly."""
    from gaca_api.config import settings

    monkeypatch.setattr(settings, "automation_alerts", False)


@pytest.fixture(autouse=True)
def _no_real_heartbeat_job(monkeypatch):
    """`hermes connect` and the installer schedule the heartbeat by running the real `hermes` CLI;
    on a machine that has Hermes, a test must never touch its real cron jobs. The heartbeat tests
    turn it back on, with a fake shell."""
    from gaca_api.config import settings

    monkeypatch.setattr(settings, "heartbeat_schedule", "")


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
