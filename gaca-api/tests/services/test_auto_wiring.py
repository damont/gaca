"""A crew member gets its GACA token with no manual step: when the app creates it, and when the app
discovers one made by hand. And the write stays inside a very small box."""
import asyncio
import stat
import sys
import textwrap

import pytest

from conftest import make_owner as _make_owner
from gaca_api.config import settings
from gaca_api.db import client
from gaca_api.provisioning import envfile
from gaca_api.provisioning.base import ProvisioningError
from gaca_api.provisioning.local import GACA_ENV_KEYS, LocalError, LocalHermes
from gaca_api.services import auth as auth_service
from gaca_api.services import tokens as token_service
from gaca_api.utils import tokens as token_utils


def run(factory):
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
    (home / "profiles" / "toolshed").mkdir(parents=True)
    (home / ".env").write_text(f"API_SERVER_ENABLED=true\nAPI_SERVER_PORT=8642\nAPI_SERVER_KEY={'d' * 32}\nOPENAI_API_KEY=keep\n")
    (home / "profiles" / "toolshed" / ".env").write_text(f"API_SERVER_ENABLED=true\nAPI_SERVER_PORT=8643\nAPI_SERVER_KEY={'t' * 32}\nANTHROPIC_API_KEY=keep\n")
    stub = tmp_path / "hermes-stub"
    stub.write_text(textwrap.dedent(f'''\
        #!{sys.executable}
        import pathlib, sys
        if sys.argv[1:3] == ["profile", "create"]:
            if sys.argv[3] == "boom":
                sys.stderr.write("profile exists"); sys.exit(1)
            (pathlib.Path({str(home)!r}) / "profiles" / sys.argv[3]).mkdir()
        else:
            print("Hermes Agent v0.21.5")
        '''))
    stub.chmod(0o755)
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "t.db"))
    monkeypatch.setattr(settings, "agent_access", "local")
    monkeypatch.setattr(settings, "hermes_home", str(home))
    monkeypatch.setattr(settings, "hermes_bin", str(stub))
    monkeypatch.setattr(settings, "api_port", 41700)
    served = {"toolshed"}

    async def fake_served(port, name):  # never touch the network from a test
        return name in served
    monkeypatch.setattr(LocalHermes, "_served_by_default", staticmethod(fake_served))
    return type("Box", (), {"home": home, "served": served})


async def make_owner():
    return await _make_owner(name="O", email="o@example.com")


def env(path):
    return dict(line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)


def run_sync(coro):
    return asyncio.run(coro)


# ── creating a crew member in the app ────────────────────────────────

def test_a_crew_member_created_in_the_app_is_wired_in_the_same_write(box):
    box.served.add("fresh")

    async def go():
        owner = await make_owner()
        user = await auth_service.discover_crew(owner)
        user = await auth_service.provision_crew_member(user, "fresh", None)
        path = box.home / "profiles" / "fresh" / ".env"
        values = env(path)
        found = await token_service.authenticate(values["GACA_API_TOKEN"])
        listed = [t for t in await token_service.list_tokens(owner) if t.name == "hermes-fresh"]
        return values, stat.S_IMODE(path.stat().st_mode), found, listed

    values, mode, found, listed = run(go)
    assert values["GACA_BASE_URL"] == "http://127.0.0.1:41700"
    assert values["API_SERVER_ENABLED"] == "true" and len(values["API_SERVER_KEY"]) == 64  # its own settings, same file
    assert mode == 0o600
    assert found is not None and sorted(found[1].scopes) == sorted(token_utils.SCOPES) and found[1].name == "hermes-fresh"
    assert len(listed) == 1


def test_a_failed_creation_does_not_leave_a_valid_token_behind(box):
    async def go():
        owner = await make_owner()
        user = await auth_service.discover_crew(owner)
        with pytest.raises(ProvisioningError):
            await auth_service.provision_crew_member(user, "boom", None)
        return [t for t in await token_service.list_tokens(owner) if t.name == "hermes-boom" and t.revoked_at is None]
    assert run(go) == []


# ── discovering a profile made by hand ───────────────────────────────

def test_discovery_wires_a_profile_that_has_no_token_and_leaves_the_rest_alone(box):
    (box.home / "profiles" / "handmade").mkdir()
    (box.home / "profiles" / "handmade" / ".env").write_text("ANTHROPIC_API_KEY=keep\n")
    default_before = (box.home / ".env").read_text()

    async def go():
        owner = await make_owner()
        await auth_service.discover_crew(owner)
        first = {p.name: (p / ".env").read_text() for p in (box.home / "profiles").iterdir()}
        await auth_service.discover_crew(owner)  # second discovery: nothing new to wire
        second = {p.name: (p / ".env").read_text() for p in (box.home / "profiles").iterdir()}
        return first, second, await token_service.list_tokens(owner)

    first, second, tokens = run(go)
    assert first == second  # idempotent: no rotation, no churn
    assert "GACA_API_TOKEN=gaca_pat_" in first["handmade"] and "ANTHROPIC_API_KEY=keep" in first["handmade"]
    assert "GACA_API_TOKEN=gaca_pat_" in first["toolshed"] and "ANTHROPIC_API_KEY=keep" in first["toolshed"]
    assert (box.home / ".env").read_text() == default_before  # the default profile is outside what we may write
    assert sorted(t.name for t in tokens) == ["hermes-handmade", "hermes-toolshed"]


def test_a_profile_that_already_has_a_token_is_not_touched(box):
    envp = box.home / "profiles" / "toolshed" / ".env"
    envp.write_text(envp.read_text() + "GACA_API_TOKEN=gaca_pat_already-there-keep-me\nGACA_BASE_URL=http://127.0.0.1:41700\n")
    before = envp.read_text()
    run(lambda: _discover())
    assert envp.read_text() == before


async def _discover():
    owner = await make_owner()
    await auth_service.discover_crew(owner)


def test_wiring_failure_never_fails_discovery_or_leaks_a_token(box, monkeypatch):
    async def refuse(self, name, values):
        raise LocalError("disk full")
    monkeypatch.setattr(LocalHermes, "write_gaca_env", refuse)

    async def go():
        owner = await make_owner()
        user = await auth_service.discover_crew(owner)  # still succeeds
        return {c.name for c in user.crew}, [t for t in await token_service.list_tokens(owner) if t.revoked_at is None]
    crew, live = run(go)
    assert crew == {"default", "toolshed"} and live == []


def test_ssh_mode_never_tries_to_wire(box, monkeypatch):
    from gaca_api.provisioning.ssh import SshProvisioner
    from gaca_api.models.user import SSHConnection
    p = SshProvisioner(SSHConnection(host="h", username="u", private_key="k"))
    assert p.can_wire_gaca is False
    assert run_sync(p.profiles_missing_gaca_token()) == []
    with pytest.raises(ProvisioningError):
        run_sync(p.write_gaca_env("x", {"GACA_API_TOKEN": "t"}))


# ── the write stays in its box ───────────────────────────────────────

def test_only_the_two_gaca_keys_can_be_written_into_a_profile(box):
    hermes = LocalHermes(box.home, "hermes")
    for bad in ({}, {"API_SERVER_KEY": "x" * 32}, {"GACA_API_TOKEN": "t", "OPENAI_API_KEY": "x"}, {"PATH": "/tmp"}):
        with pytest.raises(LocalError):
            run_sync(hermes.write_gaca_env("toolshed", bad))
        with pytest.raises(LocalError):
            run_sync(hermes.create_profile("fresh", 1, "k" * 32, extra_env=bad))
    assert GACA_ENV_KEYS == ("GACA_BASE_URL", "GACA_API_TOKEN")
    assert "OPENAI_API_KEY" not in (box.home / "profiles" / "toolshed" / ".env").read_text()


@pytest.mark.parametrize("name", ["default", "..", "../x", "a/b", "", "x" * 65, "a b", "toolshed\n"])
def test_wiring_refuses_bad_names_and_never_the_default_profile(box, name):
    hermes = LocalHermes(box.home, "hermes")
    with pytest.raises(LocalError):
        run_sync(hermes.write_gaca_env(name, {"GACA_API_TOKEN": "gaca_pat_x"}))


def test_wiring_will_not_follow_a_symlinked_profile_or_env(box, tmp_path):
    hermes = LocalHermes(box.home, "hermes")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / ".env").write_text("UNTOUCHED=1\n")
    (box.home / "profiles" / "linked").symlink_to(elsewhere)
    with pytest.raises(LocalError):
        run_sync(hermes.write_gaca_env("linked", {"GACA_API_TOKEN": "gaca_pat_x"}))
    assert (elsewhere / ".env").read_text() == "UNTOUCHED=1\n"
    assert "linked" not in run_sync(hermes.profiles_missing_gaca_token())

    target = tmp_path / "shared.env"
    target.write_text("SHARED=1\n")
    envp = box.home / "profiles" / "toolshed" / ".env"
    envp.unlink()
    envp.symlink_to(target)
    run_sync(hermes.write_gaca_env("toolshed", {"GACA_API_TOKEN": "gaca_pat_abc", "GACA_BASE_URL": "http://127.0.0.1:1"}))
    assert target.read_text() == "SHARED=1\n" and not envp.is_symlink()


def test_has_gaca_token_ignores_old_jwts_and_comments(tmp_path):
    p = tmp_path / ".env"
    p.write_text("GACA_API_TOKEN=eyJhbGciOi.old.jwt\n")
    assert envfile.has_gaca_token(p) is False
    p.write_text("# GACA_API_TOKEN=gaca_pat_commented\nOTHER=1\n")
    assert envfile.has_gaca_token(p) is False
    p.write_text("OTHER=1\nGACA_API_TOKEN=gaca_pat_real\n")
    assert envfile.has_gaca_token(p) is True


# ── GACA's skills ────────────────────────────────────────────────────

def skill_file(box, profile, skill="gaca-alerts", name="SKILL.md"):
    return box.home / "profiles" / profile / "skills" / "gaca" / skill / name


def test_a_crew_member_created_in_the_app_gets_the_gaca_skills(box):
    box.served.add("fresh")

    async def go():
        owner = await make_owner()
        user = await auth_service.discover_crew(owner)
        await auth_service.provision_crew_member(user, "fresh", None)
    run(go)
    for skill in ("gaca", "gaca-alerts", "gaca-briefing", "gaca-lists", "gaca-artifacts"):
        assert skill_file(box, "fresh", skill).is_file(), skill
    assert skill_file(box, "fresh", "gaca-alerts", "scripts/gaca.py").is_file()


def test_discovery_gives_every_named_profile_the_skills_but_never_touches_default(box):
    (box.home / "profiles" / "handmade").mkdir()

    async def go():
        owner = await make_owner()
        await auth_service.discover_crew(owner)
    run(go)
    assert skill_file(box, "toolshed").is_file() and skill_file(box, "handmade").is_file()
    assert not (box.home / "skills").exists()  # the default profile is the installer's job, not the service's


def test_discovery_does_not_overwrite_a_skill_the_agent_edited(box):
    async def go():
        owner = await make_owner()
        await auth_service.discover_crew(owner)
        mine = skill_file(box, "toolshed")
        mine.write_text("my tweak")
        await auth_service.discover_crew(owner)
        return mine.read_text()
    assert run(go) == "my tweak"


def test_a_skills_failure_never_fails_discovery_or_creation(box, monkeypatch):
    box.served.add("fresh")
    (box.home / "profiles" / "toolshed" / "skills").symlink_to(box.home)  # an unsafe target

    async def go():
        owner = await make_owner()
        user = await auth_service.discover_crew(owner)  # toolshed's skills dir is a symlink: refused, quietly
        return await auth_service.provision_crew_member(user, "fresh", None)
    user = run(go)
    assert any(c.name == "fresh" for c in user.crew)
    assert not any((box.home / "skills").iterdir()) if (box.home / "skills").exists() else True  # nothing escaped via the link


def test_install_skills_refuses_default_odd_names_and_missing_profiles(box):
    hermes = LocalHermes(box.home, settings.hermes_bin)
    for bad in ("default", "../x", "a/b", "", "x" * 80):
        with pytest.raises(LocalError):
            run_sync(hermes.install_skills(bad))
    with pytest.raises(LocalError):
        run_sync(hermes.install_skills("nosuch"))


def test_install_skills_refuses_a_symlinked_profile_dir(box, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (box.home / "profiles" / "linked").symlink_to(outside)
    with pytest.raises(LocalError):
        run_sync(LocalHermes(box.home, settings.hermes_bin).install_skills("linked"))
    assert list(outside.iterdir()) == []
