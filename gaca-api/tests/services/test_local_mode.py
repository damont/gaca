"""GACA_AGENT_ACCESS=local at the service level: discovery and creation need no SSH login, a
login sent anyway is ignored (never stored), and nothing here can reach a real Hermes."""
import asyncio
import sys
import textwrap

import pytest

from gaca_api.config import settings
from gaca_api.db import client
from gaca_api.models.user import UserDoc
from gaca_api.provisioning.local import LocalHermes
from gaca_api.provisioning.select import provisioner_for
from gaca_api.routers.crew import crew_out
from gaca_api.schemas.auth import CrewAccess
from gaca_api.services import auth as auth_service


def _run(factory):
    async def main():
        await client.connect()
        try:
            return await factory()
        finally:
            await client.close()
    return asyncio.run(main())


@pytest.fixture
def local_box(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    (home / "profiles" / "toolshed").mkdir(parents=True)
    (home / ".env").write_text(f"API_SERVER_ENABLED=true\nAPI_SERVER_PORT=8642\nAPI_SERVER_KEY={'d' * 32}\n")
    (home / "profiles" / "toolshed" / ".env").write_text(
        f"API_SERVER_ENABLED=true\nAPI_SERVER_PORT=8643\nAPI_SERVER_KEY={'t' * 32}\n")
    stub = tmp_path / "hermes-stub"
    stub.write_text(textwrap.dedent(f'''\
        #!{sys.executable}
        import pathlib, sys
        if sys.argv[1:3] == ["profile", "create"]:
            (pathlib.Path({str(home)!r}) / "profiles" / sys.argv[3]).mkdir()
        else:
            print("Hermes Agent v0.21.5")
        '''))
    stub.chmod(0o755)
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "t.db"))
    monkeypatch.setattr(settings, "agent_access", "local")
    monkeypatch.setattr(settings, "hermes_home", str(home))
    monkeypatch.setattr(settings, "hermes_bin", str(stub))

    served = {"toolshed"}

    async def fake_served(port, name):  # never touch the network from a test
        return name in served
    monkeypatch.setattr(LocalHermes, "_served_by_default", staticmethod(fake_served))
    return served


async def _user() -> UserDoc:
    return await auth_service.create_user(name="D", email="d@example.com", password="pw-12345678")


def test_a_user_with_no_ssh_login_can_create_and_discover_in_local_mode(local_box):
    async def go():
        user = await _user()
        assert user.ssh is None
        assert provisioner_for(user) is not None
        assert crew_out(user).can_create is True
    _run(go)


def test_discovery_registers_profiles_and_defaults_the_host_to_loopback(local_box):
    async def go():
        updated = await auth_service.discover_crew(await _user())
        assert {c.name for c in updated.crew} == {"default", "toolshed"}
        assert updated.agent_host == "http://127.0.0.1"
        assert updated.default_crew == "default"
        toolshed = next(c for c in updated.crew if c.name == "toolshed")
        assert (toolshed.port, toolshed.path) == (8642, "/p/toolshed")
    _run(go)


def test_an_ssh_login_sent_in_local_mode_is_ignored_and_never_stored(local_box):
    access = CrewAccess(host="evil.example.com", port=22, username="root", private_key="-----BEGIN KEY-----")

    async def go():
        user = await auth_service.discover_crew(await _user(), access)
        assert user.ssh is None
        assert user.agent_host == "http://127.0.0.1"  # not http://evil.example.com
        stored = await auth_service.find_by_id(user.id)
        assert stored.ssh is None and "evil.example.com" not in stored.model_dump_json()
        created = await auth_service.provision_crew_member(user, "fresh", None, access)
        assert created.ssh is None and "BEGIN KEY" not in created.model_dump_json()
    local_box.add("fresh")
    _run(go)


def test_provisioning_in_local_mode_creates_the_profile_and_registers_it(local_box):
    local_box.add("fresh")

    async def go():
        user = await auth_service.discover_crew(await _user())
        updated = await auth_service.provision_crew_member(user, "fresh", None)
        member = next(c for c in updated.crew if c.name == "fresh")
        assert (member.port, member.path) == (8642, "/p/fresh") and len(member.api_key) == 64
    _run(go)


def test_ssh_mode_still_needs_a_login(local_box, monkeypatch):
    monkeypatch.setattr(settings, "agent_access", "ssh")

    async def go():
        user = await _user()
        assert provisioner_for(user) is None and crew_out(user).can_create is False
    _run(go)
