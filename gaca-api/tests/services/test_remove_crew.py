"""Removing a crew member removes it for real: the profile on the Hermes box, its GACA token, and
the entry, and discovery must NOT bring it back (the original bug)."""
import asyncio
import sys
import textwrap

import pytest

from conftest import make_owner
from gaca_api.config import settings
from gaca_api.db import client
from gaca_api.db import users as users_db
from gaca_api.provisioning.base import ProvisioningError
from gaca_api.provisioning.local import LocalHermes
from gaca_api.services import auth as auth_service
from gaca_api.services import tokens as token_service
from gaca_api.services.exceptions import InvalidRequest, NotFound


def _run(factory):
    async def main():
        await client.connect()
        try:
            return await factory()
        finally:
            await client.close()
    return asyncio.run(main())


@pytest.fixture
def box(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    for name, port in (("toolshed", 8643), ("david", 8644)):
        (home / "profiles" / name).mkdir(parents=True)
        (home / "profiles" / name / ".env").write_text(
            f"API_SERVER_ENABLED=true\nAPI_SERVER_PORT={port}\nAPI_SERVER_KEY={name[0] * 32}\n")
    (home / ".env").write_text(f"API_SERVER_ENABLED=true\nAPI_SERVER_PORT=8642\nAPI_SERVER_KEY={'d' * 32}\n")
    mode = tmp_path / "mode"
    mode.write_text("delete")
    stub = tmp_path / "hermes-stub"
    stub.write_text(textwrap.dedent(f'''\
        #!{sys.executable}
        import pathlib, shutil, sys
        if sys.argv[1:3] == ["profile", "delete"]:
            if pathlib.Path({str(mode)!r}).read_text() == "fail":
                print("boom", file=sys.stderr); sys.exit(1)
            shutil.rmtree(pathlib.Path({str(home)!r}) / "profiles" / sys.argv[3])
        else:
            print("Hermes Agent v0.21.5")
        '''))
    stub.chmod(0o755)
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "t.db"))
    monkeypatch.setattr(settings, "agent_access", "local")
    monkeypatch.setattr(settings, "hermes_home", str(home))
    monkeypatch.setattr(settings, "hermes_bin", str(stub))

    async def never_served(port, name):
        return False
    monkeypatch.setattr(LocalHermes, "_served_by_default", staticmethod(never_served))
    return type("Box", (), {"home": home, "mode": mode})


async def _registered():
    user = await make_owner()
    return await auth_service.ensure_crew_discovered(user)


def test_removing_deletes_the_profile_revokes_its_token_and_discovery_does_not_bring_it_back(box):
    async def go():
        user = await _registered()
        assert {c.name for c in user.crew} == {"default", "toolshed", "david"}
        minted, _ = await token_service.create(user, "hermes-david", ["alerts"])
        after = await auth_service.remove_crew_member(user, "david")
        # ...and the next listing, which is what used to undo it:
        again = await auth_service.ensure_crew_discovered(after)
        return after, again, await token_service.list_tokens(after), minted

    after, again, tokens, minted = _run(go)
    assert not (box.home / "profiles" / "david").exists()
    assert {c.name for c in after.crew} == {"default", "toolshed"}
    assert {c.name for c in again.crew} == {"default", "toolshed"}  # still gone after rediscovery
    assert next(t for t in tokens if t.id == minted.id).revoked_at is not None
    assert auth_service.removed_crew_names(again) == {"david"}


def test_if_the_box_refuses_nothing_else_changes(box):
    async def go():
        user = await _registered()
        minted, _ = await token_service.create(user, "hermes-david", ["alerts"])
        box.mode.write_text("fail")
        with pytest.raises(ProvisioningError, match="boom"):
            await auth_service.remove_crew_member(user, "david")
        fresh = await users_db.find(user.id)
        tokens = await token_service.list_tokens(user)
        return fresh, next(t for t in tokens if t.id == minted.id)

    fresh, token = _run(go)
    assert "david" in {c.name for c in fresh.crew} and fresh.removed_crew == []
    assert (box.home / "profiles" / "david").exists() and token.revoked_at is None


def test_a_profile_already_gone_from_the_box_is_still_removed_from_gaca(box):
    async def go():
        user = await _registered()
        import shutil
        shutil.rmtree(box.home / "profiles" / "david")
        return await auth_service.remove_crew_member(user, "david")

    assert "david" not in {c.name for c in _run(go).crew}


def test_the_default_hermes_profile_cannot_be_deleted(box):
    async def go():
        user = await _registered()
        user = await auth_service.set_default_crew_member(user, "toolshed")  # so the default-member rule isn't what blocks it
        with pytest.raises(InvalidRequest, match="cannot be deleted"):
            await auth_service.remove_crew_member(user, "default")

    _run(go)
    assert box.home.exists()


def test_an_unknown_member_is_not_found_and_touches_nothing(box):
    async def go():
        user = await _registered()
        with pytest.raises(NotFound):
            await auth_service.remove_crew_member(user, "ghost")

    _run(go)
    assert (box.home / "profiles" / "david").exists()


def test_adding_a_removed_name_back_makes_it_not_removed_any_more(box):
    async def go():
        user = await _registered()
        user = await auth_service.remove_crew_member(user, "david")
        assert auth_service.removed_crew_names(user) == {"david"}
        (box.home / "profiles" / "david").mkdir()
        (box.home / "profiles" / "david" / ".env").write_text(
            f"API_SERVER_ENABLED=true\nAPI_SERVER_PORT=8644\nAPI_SERVER_KEY={'x' * 32}\n")
        return await auth_service.ensure_crew_discovered(user)

    back = _run(go)
    assert "david" in {c.name for c in back.crew} and auth_service.removed_crew_names(back) == set()
