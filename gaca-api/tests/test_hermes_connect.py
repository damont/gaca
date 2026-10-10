"""`gaca-api token ...` and `gaca-api hermes connect`: mint scoped tokens and write them into
Hermes' profiles without anyone copying a key. The tests lay traps (symlinks, odd names, stray
lines) because this command writes into files that hold a person's LLM provider keys."""
import asyncio
import os
import re
import stat

import pytest

from gaca_api import cli
from gaca_api.config import settings
from gaca_api.db import client
from gaca_api.services import auth as auth_service
from gaca_api.services import tokens as token_service
from gaca_api.utils import tokens as token_utils

PROVIDER_LINE = "OPENAI_API_KEY=sk-provider-must-survive"


def run(coro_fn):
    async def go():
        await client.connect()
        try:
            return await coro_fn()
        finally:
            await client.close()
    return asyncio.run(go())


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "gaca.db"))
    monkeypatch.setattr(settings, "api_port", 41700)

    async def init():
        await auth_service.create_user(name="O", email="o@example.com", password="correct horse")
    run(init)
    return tmp_path


@pytest.fixture
def hermes_home(tmp_path):
    home = tmp_path / "hermes"
    (home / "profiles" / "toolshed").mkdir(parents=True)
    (home / "profiles" / "trading").mkdir()
    (home / ".env").write_text(f"{PROVIDER_LINE}\nGACA_API_TOKEN=old-jwt-value\nOTHER=1\n")
    (home / "profiles" / "toolshed" / ".env").write_text("ANTHROPIC_API_KEY=keep-me\n")
    return home


def env(path):
    return dict(line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)


def active_tokens():
    async def go():
        owner = await __import__("gaca_api.db.users", fromlist=["x"]).first()
        return [t for t in await token_service.list_tokens(owner) if t.revoked_at is None]
    return run(go)


# ── token create / list / revoke ─────────────────────────────────────

def test_token_create_prints_the_secret_once_and_list_never_does(server, capsys):
    assert cli.main(["token", "create", "--name", "ci", "--scopes", "alerts,lists"]) == 0
    out = capsys.readouterr().out
    secret = re.search(r"gaca_pat_[A-Za-z0-9_-]+", out).group(0)
    assert "alerts, lists" in out
    assert cli.main(["token", "list"]) == 0
    listing = capsys.readouterr().out
    assert "ci" in listing and "active" in listing and "never" in listing and secret not in listing


def test_token_create_rejects_unknown_scopes_and_needs_an_owner(server, tmp_path, monkeypatch, capsys):
    assert cli.main(["token", "create", "--name", "x", "--scopes", "alerts,root"]) == 1
    assert "scopes must be" in capsys.readouterr().err
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "empty.db"))
    run(lambda: asyncio.sleep(0))  # initialise an empty, unclaimed database
    assert cli.main(["token", "create", "--name", "x"]) == 1
    assert "no owner" in capsys.readouterr().err


def test_token_revoke(server, capsys):
    cli.main(["token", "create", "--name", "ci"])
    token_id = re.search(r"Token (\w+) ", capsys.readouterr().out).group(1)
    assert cli.main(["token", "revoke", token_id]) == 0
    assert cli.main(["token", "revoke", "nope"]) == 1
    cli.main(["token", "list"])
    assert "revoked" in capsys.readouterr().out


# ── hermes connect ───────────────────────────────────────────────────

def test_connect_gives_every_profile_its_own_scoped_token(server, hermes_home, capsys):
    assert cli.main(["hermes", "connect", "--hermes-home", str(hermes_home)]) == 0
    out = capsys.readouterr().out
    assert "gaca_pat_" not in out  # the command line never shows a secret

    files = {"default": hermes_home / ".env", "toolshed": hermes_home / "profiles/toolshed/.env",
             "trading": hermes_home / "profiles/trading/.env"}
    secrets = {}
    for name, path in files.items():
        values = env(path)
        assert values["GACA_BASE_URL"] == "http://127.0.0.1:41700"
        assert values["GACA_API_TOKEN"].startswith(token_utils.PREFIX)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        secrets[name] = values["GACA_API_TOKEN"]
    assert len(set(secrets.values())) == 3  # one per profile, so one can be revoked alone

    # Everything else in those files is untouched — including the old JWT being replaced in place.
    assert PROVIDER_LINE in files["default"].read_text() and env(files["default"])["OTHER"] == "1"
    assert "old-jwt-value" not in files["default"].read_text()
    assert env(files["toolshed"])["ANTHROPIC_API_KEY"] == "keep-me"

    async def check():
        found = [await token_service.authenticate(s) for s in secrets.values()]
        return [(u.email, sorted(d.scopes), d.name) for u, d in found]
    checked = run(check)
    assert {name for _, _, name in checked} == {"hermes-default", "hermes-toolshed", "hermes-trading"}
    assert all(email == "o@example.com" and scopes == sorted(token_utils.SCOPES) for email, scopes, _ in checked)


def test_connect_again_rotates_instead_of_piling_up_valid_tokens(server, hermes_home):
    cli.main(["hermes", "connect", "--hermes-home", str(hermes_home)])
    first = env(hermes_home / ".env")["GACA_API_TOKEN"]
    cli.main(["hermes", "connect", "--hermes-home", str(hermes_home)])
    second = env(hermes_home / ".env")["GACA_API_TOKEN"]
    assert first != second
    assert len(active_tokens()) == 3
    assert run(lambda: token_service.authenticate(first)) is None
    assert run(lambda: token_service.authenticate(second)) is not None


def test_connect_skips_symlinked_and_oddly_named_profiles(server, hermes_home, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / ".env").write_text("UNTOUCHED=1\n")
    (hermes_home / "profiles" / "linked").symlink_to(elsewhere)
    (hermes_home / "profiles" / "bad name").mkdir()
    (hermes_home / "profiles" / "..hidden").mkdir()
    cli.main(["hermes", "connect", "--hermes-home", str(hermes_home)])
    assert (elsewhere / ".env").read_text() == "UNTOUCHED=1\n"
    assert not (hermes_home / "profiles" / "bad name" / ".env").exists()
    assert not (hermes_home / "profiles" / "..hidden" / ".env").exists()
    assert {t.name for t in active_tokens()} == {"hermes-default", "hermes-toolshed", "hermes-trading"}


def test_connect_replaces_a_symlinked_env_instead_of_writing_through_it(server, hermes_home, tmp_path):
    target = tmp_path / "shared.env"
    target.write_text("SHARED=1\n")
    env_path = hermes_home / "profiles" / "trading" / ".env"
    env_path.symlink_to(target)
    cli.main(["hermes", "connect", "--hermes-home", str(hermes_home)])
    assert target.read_text() == "SHARED=1\n"
    assert not env_path.is_symlink() and "GACA_API_TOKEN" in env_path.read_text()


def test_connect_honours_a_custom_base_url_and_refuses_unsafe_values(server, hermes_home):
    assert cli.main(["hermes", "connect", "--hermes-home", str(hermes_home), "--base-url", "http://127.0.0.1:9999"]) == 0
    assert env(hermes_home / ".env")["GACA_BASE_URL"] == "http://127.0.0.1:9999"
    before = (hermes_home / ".env").read_text()
    with pytest.raises(ValueError, match="unsafe"):
        cli.main(["hermes", "connect", "--hermes-home", str(hermes_home), "--base-url", "http://x\nEVIL=1"])
    assert (hermes_home / ".env").read_text() == before  # nothing half-written


def test_a_failed_write_leaves_the_old_token_working(server, hermes_home):
    cli.main(["hermes", "connect", "--hermes-home", str(hermes_home)])
    old = env(hermes_home / ".env")["GACA_API_TOKEN"]
    with pytest.raises(ValueError):
        cli.main(["hermes", "connect", "--hermes-home", str(hermes_home), "--base-url", "bad url"])
    assert run(lambda: token_service.authenticate(old)) is not None  # still valid: revoke comes after the write
    assert len(active_tokens()) == 3  # and the token minted for the failed write was revoked, not orphaned


def test_connect_needs_a_hermes_home_and_an_owner(server, tmp_path, capsys):
    assert cli.main(["hermes", "connect", "--hermes-home", str(tmp_path / "missing")]) == 1
    assert "No Hermes home" in capsys.readouterr().err
