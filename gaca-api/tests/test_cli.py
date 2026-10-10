"""The owner tools (`gaca-api setup-code | status | reset-password`)."""
import asyncio
from pathlib import Path

import pytest

from gaca_api import cli
from gaca_api.config import settings
from gaca_api.db import client
from gaca_api.db import users as users_db
from gaca_api.services import auth as auth_service
from gaca_api.services import setup as setup_service
from gaca_api.utils.auth import verify_password


@pytest.fixture
def server(tmp_path, monkeypatch):
    """An initialised, unclaimed server database."""
    db = tmp_path / "gaca.db"
    monkeypatch.setattr(settings, "db_path", str(db))

    async def init():
        await client.connect()
        await client.close()
    asyncio.run(init())
    return db


def test_it_refuses_to_invent_a_database(tmp_path, monkeypatch, capsys):
    ghost = tmp_path / "nowhere" / "gaca.db"
    monkeypatch.setattr(settings, "db_path", str(ghost))
    assert cli.main(["setup-code"]) == 2
    assert not ghost.exists() and not ghost.parent.exists()
    assert "No database at" in capsys.readouterr().err


def test_setup_code_prints_a_usable_code_and_an_app_link(server, capsys):
    assert cli.main(["setup-code", "--url", "https://gaca.example.com/"]) == 0
    out = capsys.readouterr().out
    code = out.split("Setup code:")[1].split()[0]
    assert len(code.replace("-", "")) == setup_service.CODE_LENGTH
    assert "gaca://setup?url=https%3A%2F%2Fgaca.example.com&code=" + code.replace("-", "") in out

    async def claim():
        await client.connect()
        try:
            return await setup_service.claim_with_password(code, "O", "o@example.com", "long enough pw")
        finally:
            await client.close()
    assert asyncio.run(claim()).email == "o@example.com"


def test_a_new_code_replaces_the_old_one(server, capsys):
    cli.main(["setup-code"]); first = capsys.readouterr().out.split("Setup code:")[1].split()[0]
    cli.main(["setup-code"]); second = capsys.readouterr().out.split("Setup code:")[1].split()[0]

    async def try_code(code):
        await client.connect()
        try:
            await setup_service._check_code(code)
            return True
        except setup_service.SetupRefused:
            return False
        finally:
            await client.close()
    assert asyncio.run(try_code(second)) is True
    assert asyncio.run(try_code(first)) is False


def test_no_code_is_issued_once_the_server_has_an_owner(server, capsys):
    async def make_owner():
        await client.connect()
        await auth_service.create_user(name="O", email="o@example.com", password="long enough pw")
        await client.close()
    asyncio.run(make_owner())
    assert cli.main(["setup-code"]) == 1
    assert "already has an owner" in capsys.readouterr().err
    assert cli.main(["status"]) == 0
    assert "claimed: yes" in capsys.readouterr().out


def test_status_before_claiming(server, capsys):
    assert cli.main(["status"]) == 0
    assert "claimed: no" in capsys.readouterr().out


def test_reset_password_changes_only_the_password(server, monkeypatch, capsys):
    async def make_owner():
        await client.connect()
        await auth_service.create_user(name="O", email="o@example.com", password="old password!")
        await client.close()
    asyncio.run(make_owner())
    monkeypatch.setattr(cli, "_read_new_password", lambda: "brand new password")
    assert cli.main(["reset-password"]) == 0

    async def check():
        await client.connect()
        try:
            owner = await users_db.first()
            return owner.email, verify_password("brand new password", owner.hashed_password), \
                verify_password("old password!", owner.hashed_password)
        finally:
            await client.close()
    assert asyncio.run(check()) == ("o@example.com", True, False)


def test_reset_password_needs_an_owner(server, capsys):
    assert cli.main(["reset-password"]) == 1
    assert "no owner yet" in capsys.readouterr().err
