"""The owner tools (`gaca-api pair | device | status`). Pairing's own output is covered in test_pairing.py."""
import asyncio

import pytest

from conftest import make_owner
from gaca_api import cli
from gaca_api.config import settings
from gaca_api.db import client
from gaca_api.services import pairing as pairing_service


@pytest.fixture
def server(tmp_path, monkeypatch):
    """An initialised server database with no owner."""
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
    assert cli.main(["pair"]) == 2
    assert not ghost.exists() and not ghost.parent.exists()
    assert "No database at" in capsys.readouterr().err


def test_a_new_code_replaces_the_old_one(server, capsys):
    cli.main(["pair", "--no-qr"]); first = capsys.readouterr().out.split("Pairing code:")[1].split()[0]
    cli.main(["pair", "--no-qr"]); second = capsys.readouterr().out.split("Pairing code:")[1].split()[0]

    async def try_code(code):
        await client.connect()
        try:
            await pairing_service._check_code(code)
            return True
        except pairing_service.PairingRefused:
            return False
        finally:
            await client.close()
    assert asyncio.run(try_code(second)) is True
    assert asyncio.run(try_code(first)) is False


def test_codes_are_issued_even_with_an_owner_so_more_phones_can_pair(server, capsys):
    async def seed():
        await client.connect()
        await make_owner(name="O", email="o@example.com")
        await client.close()
    asyncio.run(seed())
    assert cli.main(["pair", "--no-qr"]) == 0
    assert "Pairing code:" in capsys.readouterr().out


def test_status_before_and_after_pairing(server, capsys):
    assert cli.main(["status"]) == 0
    assert "no owner yet" in capsys.readouterr().out

    async def pair_one():
        await client.connect()
        try:
            code, _ = await pairing_service.issue_code()
            await pairing_service.pair(code, "Pixel 7")
        finally:
            await client.close()
    asyncio.run(pair_one())
    assert cli.main(["status"]) == 0
    assert "paired devices: 1" in capsys.readouterr().out


def test_the_old_sign_in_commands_are_gone(capsys):
    for old in ("setup-code", "reset-password"):
        with pytest.raises(SystemExit):
            cli.main([old])
