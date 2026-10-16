"""Tests for the auth service: the owner record and the per-owner crew config."""
import asyncio
import json
import sqlite3

import pytest

from gaca_api.config import settings
from gaca_api.db import client
from gaca_api.db import users as users_db
from gaca_api.models.user import SSHConnection, UserDoc
from gaca_api.provisioning import ssh as ssh_provisioning
from gaca_api.services import auth as auth_service
from gaca_api.services.exceptions import CrewNotConfigured, InvalidRequest, NotFound
from conftest import make_owner


def _run(factory):
    async def main():
        await client.connect()
        try:
            return await factory()
        finally:
            await client.close()

    return asyncio.run(main())


def test_a_stored_owner_is_findable_by_id(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def main():
        await client.connect()
        try:
            created = await make_owner("David", "David@Example.com")
            return created, await auth_service.find_by_id(created.id)
        finally:
            await client.close()

    created, found = asyncio.run(main())
    assert found is not None and found.id == created.id
    assert found.display_name == "David" and found.email == "david@example.com" and found.is_active is True


def test_duplicate_email_rejected_by_the_database(tmp_path):
    """The UNIQUE index on users.email is the backstop that makes a duplicate impossible."""
    settings.db_path = str(tmp_path / "t.db")

    async def main():
        await client.connect()
        try:
            await make_owner("David", "david@example.com")
            with pytest.raises(sqlite3.IntegrityError):
                await users_db.insert(UserDoc(email="david@example.com", display_name="Impostor"))
        finally:
            await client.close()

    asyncio.run(main())


@pytest.mark.parametrize("with_google", [True, False])
def test_old_passwords_and_google_identities_are_scrubbed_on_startup(tmp_path, with_google):
    """A database from the sign-in era still holds an Argon2 hash and a Google id: they are removed
    when the server next opens it, and the rest of the owner's record is untouched."""
    db = tmp_path / "old.db"
    settings.db_path = str(db)

    async def seed():
        await client.connect()
        await client.close()
    asyncio.run(seed())

    legacy_doc = {
        "id": "u1", "email": "david@example.com", "display_name": "David",
        "hashed_password": "$argon2id$v=19$m=65536,t=3,p=4$abc$def", "google_sub": "g-123",
        "email_verified": True, "is_active": True, "created_at": "2026-01-01T00:00:00+00:00",
        "agent_host": "http://hermes-host", "crew": [], "default_crew": None,
    }
    if not with_google:  # the oldest shape: a password, and none of the Google fields at all
        del legacy_doc["google_sub"], legacy_doc["email_verified"]
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO users (id, email, google_sub, created_at, doc) VALUES (?, ?, ?, ?, ?)",
        ("u1", "david@example.com", "g-123" if with_google else None, "2026-01-01T00:00:00+00:00", json.dumps(legacy_doc)),
    )
    con.commit()
    con.close()

    asyncio.run(seed())  # the server starting up again

    con = sqlite3.connect(db)
    row = con.execute("SELECT google_sub, doc FROM users").fetchone()
    con.close()
    assert row[0] is None
    doc = json.loads(row[1])
    assert "hashed_password" not in doc and "google_sub" not in doc and "email_verified" not in doc
    assert doc["agent_host"] == "http://hermes-host" and doc["display_name"] == "David"



# ── per-user crew config ────────────────────────────────────────────

async def _new_user() -> UserDoc:
    return await make_owner(name="David", email="david@example.com")


def test_set_agent_host_persists(tmp_path):
    """Moving an already-connected agent to a new host — not first-time
    setup, that's connect_agent below."""
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        user = await _new_user()
        assert user.agent_host is None
        updated = await auth_service.set_agent_host(user, "http://hermes-host.lan")
        reloaded = await auth_service.find_by_id(user.id)
        return updated, reloaded

    updated, reloaded = _run(go)
    assert updated.agent_host == "http://hermes-host.lan"
    # persisted, not just returned
    assert reloaded.agent_host == "http://hermes-host.lan"


def test_connect_agent_sets_host_and_creates_the_first_crew_member(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        user = await _new_user()
        updated = await auth_service.connect_agent(
            user, "http://hermes-host.lan", 8642, "the-key"
        )
        reloaded = await auth_service.find_by_id(user.id)
        return updated, reloaded

    updated, reloaded = _run(go)
    for user in (updated, reloaded):
        assert user.agent_host == "http://hermes-host.lan"
        assert [c.name for c in user.crew] == ["default"]
        assert user.crew[0].port == 8642
        assert user.crew[0].api_key == "the-key"
        assert user.default_crew == "default"


def test_connect_agent_honors_a_custom_first_crew_member_name(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        user = await _new_user()
        return await auth_service.connect_agent(
            user, "http://hermes-host.lan", 8642, "the-key", name="toolshed"
        )

    updated = _run(go)
    assert updated.default_crew == "toolshed"
    assert updated.crew[0].name == "toolshed"


def test_connect_agent_rejects_a_second_connect(tmp_path):
    """One agent per account — a second connect is a real error, not a
    silent overwrite. Moving to a new host is set_agent_host; adding another
    profile on the same box is add_crew_member."""
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        user = await _new_user()
        user = await auth_service.connect_agent(user, "http://a", 8642, "key-1")
        with pytest.raises(InvalidRequest):
            await auth_service.connect_agent(user, "http://b", 8643, "key-2")

    _run(go)


def test_add_crew_member_persists_and_becomes_the_default(tmp_path):
    """The first crew member a user adds becomes their default automatically
    — a user with exactly one obviously means for it to be used."""
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        user = await _new_user()
        updated = await auth_service.add_crew_member(
            user, "toolshed", 8643, "toolshed-key"
        )
        reloaded = await auth_service.find_by_id(user.id)
        return updated, reloaded

    updated, reloaded = _run(go)
    for user in (updated, reloaded):
        assert [c.name for c in user.crew] == ["toolshed"]
        assert user.crew[0].port == 8643
        assert user.crew[0].api_key == "toolshed-key"
        assert user.default_crew == "toolshed"


def test_add_crew_member_auto_assigns_a_color(tmp_path):
    """Unless the caller picks one, each new crew member gets the first
    palette color not already used by an existing one."""
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        user = await _new_user()
        user = await auth_service.add_crew_member(user, "toolshed", 8643, "key-1")
        return await auth_service.add_crew_member(user, "trading", 8644, "key-2")

    updated = _run(go)
    colors = [c.color for c in updated.crew]
    assert colors[0] != colors[1]  # distinct, not both the schema default


def test_add_crew_member_honors_an_explicit_color(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        user = await _new_user()
        return await auth_service.add_crew_member(
            user, "toolshed", 8643, "key-1", color="#8A6A9E"
        )

    updated = _run(go)
    assert updated.crew[0].color == "#8A6A9E"


def test_add_a_second_crew_member_does_not_change_the_default(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        user = await _new_user()
        user = await auth_service.add_crew_member(user, "toolshed", 8643, "toolshed-key")
        return await auth_service.add_crew_member(user, "trading", 8644, "trading-key")

    updated = _run(go)
    assert [c.name for c in updated.crew] == ["toolshed", "trading"]
    assert updated.default_crew == "toolshed"


def test_add_crew_member_rejects_a_duplicate_name(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        user = await _new_user()
        user = await auth_service.add_crew_member(user, "toolshed", 8643, "key-1")
        with pytest.raises(InvalidRequest):
            await auth_service.add_crew_member(user, "toolshed", 8644, "key-2")

    _run(go)


def test_update_crew_member_replaces_port_and_key(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        user = await _new_user()
        user = await auth_service.add_crew_member(user, "toolshed", 8643, "old-key")
        updated = await auth_service.update_crew_member(user, "toolshed", 8645, "new-key")
        reloaded = await auth_service.find_by_id(user.id)
        return updated, reloaded

    updated, reloaded = _run(go)
    for user in (updated, reloaded):
        assert user.crew[0].port == 8645
        assert user.crew[0].api_key == "new-key"


def test_update_crew_member_keeps_its_color_unless_given_a_new_one(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        user = await _new_user()
        user = await auth_service.add_crew_member(
            user, "toolshed", 8643, "old-key", color="#8A6A9E"
        )
        return await auth_service.update_crew_member(user, "toolshed", 8645, "new-key")

    updated = _run(go)
    assert updated.crew[0].color == "#8A6A9E"


def test_update_crew_member_raises_not_found_for_an_unknown_name(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        user = await _new_user()
        with pytest.raises(NotFound):
            await auth_service.update_crew_member(user, "nonexistent", 8643, "key")

    _run(go)


def test_remove_crew_member_deletes_it(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        user = await _new_user()
        user = await auth_service.add_crew_member(user, "toolshed", 8643, "key-1")
        user = await auth_service.add_crew_member(user, "trading", 8644, "key-2")
        return await auth_service.remove_crew_member(user, "trading")

    updated = _run(go)
    assert [c.name for c in updated.crew] == ["toolshed"]


def test_remove_crew_member_rejects_deleting_the_default_while_others_remain(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        user = await _new_user()
        user = await auth_service.add_crew_member(user, "toolshed", 8643, "key-1")
        user = await auth_service.add_crew_member(user, "trading", 8644, "key-2")
        with pytest.raises(InvalidRequest):
            await auth_service.remove_crew_member(user, "toolshed")  # still the default

    _run(go)


def test_remove_crew_member_clears_the_default_when_it_was_the_last_one(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        user = await _new_user()
        user = await auth_service.add_crew_member(user, "toolshed", 8643, "key-1")
        return await auth_service.remove_crew_member(user, "toolshed")

    updated = _run(go)
    assert updated.crew == []
    assert updated.default_crew is None


def test_set_default_crew_member_changes_it(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        user = await _new_user()
        user = await auth_service.add_crew_member(user, "toolshed", 8643, "key-1")
        user = await auth_service.add_crew_member(user, "trading", 8644, "key-2")
        return await auth_service.set_default_crew_member(user, "trading")

    updated = _run(go)
    assert updated.default_crew == "trading"


def test_set_default_crew_member_raises_not_found_for_an_unknown_name(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        user = await _new_user()
        with pytest.raises(NotFound):
            await auth_service.set_default_crew_member(user, "nonexistent")

    _run(go)


def test_resolve_crew_member_prefers_the_explicit_request(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        user = await _new_user()
        user = await auth_service.add_crew_member(user, "toolshed", 8643, "key-1")
        user = await auth_service.add_crew_member(user, "trading", 8644, "key-2")
        return auth_service.resolve_crew_member(user, "trading")

    resolved = _run(go)
    assert resolved.name == "trading"


def test_resolve_crew_member_falls_back_to_the_default(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        user = await _new_user()
        user = await auth_service.add_crew_member(user, "toolshed", 8643, "key-1")
        return auth_service.resolve_crew_member(user, None)

    resolved = _run(go)
    assert resolved.name == "toolshed"


def test_resolve_crew_member_rejects_an_unknown_explicit_request(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        user = await _new_user()
        user = await auth_service.add_crew_member(user, "toolshed", 8643, "key-1")
        with pytest.raises(InvalidRequest):
            auth_service.resolve_crew_member(user, "nonexistent")

    _run(go)


def test_resolve_crew_member_raises_when_nothing_resolves(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        user = await _new_user()
        with pytest.raises(CrewNotConfigured, match="POST /api/agent/connect"):
            auth_service.resolve_crew_member(user, None)

    _run(go)


def test_resolve_crew_for_delegate_prefers_the_explicit_request(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        user = await _new_user()
        user = await auth_service.add_crew_member(user, "toolshed", 8643, "key-1")
        user = await auth_service.add_crew_member(user, "trading", 8644, "key-2")
        return auth_service.resolve_crew_for_delegate(user, "trading", "toolshed")

    resolved = _run(go)
    assert resolved.name == "trading"


def test_resolve_crew_for_delegate_rejects_an_unknown_explicit_request(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        user = await _new_user()
        user = await auth_service.add_crew_member(user, "toolshed", 8643, "key-1")
        with pytest.raises(InvalidRequest):
            auth_service.resolve_crew_for_delegate(user, "nonexistent", None)

    _run(go)


def test_resolve_crew_for_delegate_falls_back_to_the_alerts_crew(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        user = await _new_user()
        user = await auth_service.add_crew_member(user, "toolshed", 8643, "key-1")
        user = await auth_service.add_crew_member(user, "trading", 8644, "key-2")
        return auth_service.resolve_crew_for_delegate(user, None, "trading")

    resolved = _run(go)
    assert resolved.name == "trading"


def test_resolve_crew_for_delegate_falls_back_to_the_default(tmp_path):
    """An alert-crew that doesn't match any configured name is a soft miss,
    not an error — it falls through to the account's default."""
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        user = await _new_user()
        user = await auth_service.add_crew_member(user, "toolshed", 8643, "key-1")
        return auth_service.resolve_crew_for_delegate(user, None, "some-other-profile")

    resolved = _run(go)
    assert resolved.name == "toolshed"


def test_resolve_crew_for_delegate_raises_when_nothing_resolves(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        user = await _new_user()
        with pytest.raises(CrewNotConfigured, match="POST /api/agent/connect"):
            auth_service.resolve_crew_for_delegate(user, None, None)

    _run(go)


# ── SSH provisioning ─────────────────────────────────────────────────

def test_set_ssh_connection_persists(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        user = await _new_user()
        assert user.ssh is None
        updated = await auth_service.set_ssh_connection(
            user, "hermes-host.lan", 22, "david", "-----BEGIN KEY-----\nfake\n"
        )
        reloaded = await auth_service.find_by_id(user.id)
        return updated, reloaded

    updated, reloaded = _run(go)
    for user in (updated, reloaded):
        assert user.ssh.host == "hermes-host.lan"
        assert user.ssh.port == 22
        assert user.ssh.username == "david"






# ── discovery ────────────────────────────────────────────────────────
# Unit-level here (discover_profiles itself is monkeypatched) — the real
# script is already proven end-to-end against a fake filesystem in
# tests/provisioning/test_ssh.py; these tests are about the merge logic:
# what discover_crew does with what discovery finds.

def _install_fake_discovery(monkeypatch, found: dict):
    async def fake_discover_profiles(conn):
        return found

    monkeypatch.setattr(
        ssh_provisioning, "discover_profiles", fake_discover_profiles
    )


async def _connected_user() -> UserDoc:
    user = await _new_user()
    return await auth_service.set_ssh_connection(user, "hermes-host", 22, "david", "k")


def test_discover_crew_raises_crew_not_configured_without_ssh(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        user = await _new_user()
        with pytest.raises(CrewNotConfigured, match="discover crew members"):
            await auth_service.discover_crew(user)

    _run(go)


def test_discover_crew_adds_every_profile_found(tmp_path, monkeypatch):
    settings.db_path = str(tmp_path / "t.db")
    _install_fake_discovery(monkeypatch, {
        "default": {"port": 8642, "api_key": "default-key"},
        "toolshed": {"port": 8643, "api_key": "toolshed-key"},
        "trading": {"port": 8644, "api_key": "trading-key"},
    })

    async def go():
        user = await _connected_user()
        return await auth_service.discover_crew(user)

    updated = _run(go)
    assert {c.name: (c.port, c.api_key) for c in updated.crew} == {
        "default": (8642, "default-key"),
        "toolshed": (8643, "toolshed-key"),
        "trading": (8644, "trading-key"),
    }


def test_discover_crew_gives_each_new_member_a_distinct_color(tmp_path, monkeypatch):
    settings.db_path = str(tmp_path / "t.db")
    _install_fake_discovery(monkeypatch, {
        "default": {"port": 8642, "api_key": "k1"},
        "toolshed": {"port": 8643, "api_key": "k2"},
    })

    async def go():
        user = await _connected_user()
        return await auth_service.discover_crew(user)

    updated = _run(go)
    colors = [c.color for c in updated.crew]
    assert len(set(colors)) == len(colors)


def test_discover_crew_sets_agent_host_from_the_ssh_host_when_unset(tmp_path, monkeypatch):
    settings.db_path = str(tmp_path / "t.db")
    _install_fake_discovery(monkeypatch, {"default": {"port": 8642, "api_key": "k"}})

    async def go():
        user = await _connected_user()
        assert user.agent_host is None
        return await auth_service.discover_crew(user)

    updated = _run(go)
    assert updated.agent_host == "http://hermes-host"


def test_discover_crew_leaves_an_existing_agent_host_alone(tmp_path, monkeypatch):
    settings.db_path = str(tmp_path / "t.db")
    _install_fake_discovery(monkeypatch, {"default": {"port": 8642, "api_key": "k"}})

    async def go():
        user = await _connected_user()
        user = await auth_service.set_agent_host(user, "http://somewhere-else")
        return await auth_service.discover_crew(user)

    updated = _run(go)
    assert updated.agent_host == "http://somewhere-else"


def test_discover_crew_defaults_to_the_default_named_profile(tmp_path, monkeypatch):
    settings.db_path = str(tmp_path / "t.db")
    _install_fake_discovery(monkeypatch, {
        "default": {"port": 8642, "api_key": "k1"},
        "toolshed": {"port": 8643, "api_key": "k2"},
    })

    async def go():
        user = await _connected_user()
        return await auth_service.discover_crew(user)

    updated = _run(go)
    assert updated.default_crew == "default"


def test_discover_crew_never_overwrites_an_already_known_member(tmp_path, monkeypatch):
    """A name collision keeps the existing entry — discovery adds what's
    missing, it doesn't reconcile what's already there."""
    settings.db_path = str(tmp_path / "t.db")
    _install_fake_discovery(monkeypatch, {
        "toolshed": {"port": 9999, "api_key": "from-the-box"},
    })

    async def go():
        user = await _connected_user()
        user = await auth_service.add_crew_member(user, "toolshed", 8643, "already-here")
        return await auth_service.discover_crew(user)

    updated = _run(go)
    assert len(updated.crew) == 1
    assert updated.crew[0].port == 8643
    assert updated.crew[0].api_key == "already-here"


# ── provisioning a brand new crew member ────────────────────────────
# Unit-level here too — next_free_port/create_profile are monkeypatched;
# the real remote script is proven end-to-end in
# tests/provisioning/test_ssh.py. These are about provision_crew_member's
# own orchestration: what it checks, what order, what it persists.

def _install_fake_provisioning(
    monkeypatch, *, port: int, calls: list | None = None, address: tuple | None = None
):
    """`address` is what create_profile reports back; by default the port it was asked for
    with no path (a gateway of its own)."""
    async def fake_next_free_port(conn):
        return port

    async def fake_create_profile(conn, name, port_, api_key):
        if calls is not None:
            calls.append((name, port_, api_key))
        return address or (port_, "")

    monkeypatch.setattr(ssh_provisioning, "next_free_port", fake_next_free_port)
    monkeypatch.setattr(ssh_provisioning, "create_profile", fake_create_profile)


def test_provision_crew_member_raises_crew_not_configured_without_ssh(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        user = await _new_user()
        with pytest.raises(CrewNotConfigured, match="create a crew member"):
            await auth_service.provision_crew_member(user, "toolshed")

    _run(go)


def test_provision_crew_member_rejects_a_duplicate_name_before_touching_the_box(
    tmp_path, monkeypatch
):
    settings.db_path = str(tmp_path / "t.db")
    calls: list = []
    _install_fake_provisioning(monkeypatch, port=8643, calls=calls)

    async def go():
        user = await _connected_user()
        user = await auth_service.add_crew_member(user, "toolshed", 1111, "k")
        with pytest.raises(InvalidRequest):
            await auth_service.provision_crew_member(user, "toolshed")

    _run(go)
    assert calls == []  # never reached create_profile


def test_provision_crew_member_creates_and_registers_it(tmp_path, monkeypatch):
    settings.db_path = str(tmp_path / "t.db")
    calls: list = []
    _install_fake_provisioning(monkeypatch, port=8643, calls=calls)

    async def go():
        user = await _connected_user()
        return await auth_service.provision_crew_member(user, "toolshed")

    updated = _run(go)
    assert len(calls) == 1
    name, port, api_key = calls[0]
    assert name == "toolshed"
    assert port == 8643
    assert len(api_key) >= 32  # a real generated secret, not a placeholder

    member = updated.crew[0]
    assert member.name == "toolshed"
    assert member.port == 8643
    assert member.api_key == api_key
    assert updated.default_crew == "toolshed"  # first crew member on this account


def test_provision_crew_member_honors_a_given_color(tmp_path, monkeypatch):
    settings.db_path = str(tmp_path / "t.db")
    _install_fake_provisioning(monkeypatch, port=8643)

    async def go():
        user = await _connected_user()
        return await auth_service.provision_crew_member(user, "toolshed", color="#8A6A9E")

    updated = _run(go)
    assert updated.crew[0].color == "#8A6A9E"


# ── granting access inline, on the same call, instead of a separate step ──

def test_provision_crew_member_accepts_access_inline_on_a_fresh_account(
    tmp_path, monkeypatch
):
    """No prior "grant access" call needed — a brand-new, never-connected
    account can create its first crew member in one call."""
    settings.db_path = str(tmp_path / "t.db")
    _install_fake_provisioning(monkeypatch, port=8642)

    async def go():
        user = await _new_user()
        assert user.ssh is None
        access = SSHConnection(
            host="hermes-host.lan", port=22, username="david", private_key="k"
        )
        return await auth_service.provision_crew_member(user, "default", access=access)

    updated = _run(go)
    assert updated.ssh.host == "hermes-host.lan"
    assert updated.crew[0].name == "default"


def test_provision_crew_member_remembers_access_for_next_time(tmp_path, monkeypatch):
    settings.db_path = str(tmp_path / "t.db")
    _install_fake_provisioning(monkeypatch, port=8642)

    async def go():
        user = await _new_user()
        access = SSHConnection(
            host="hermes-host.lan", port=22, username="david", private_key="k"
        )
        user = await auth_service.provision_crew_member(user, "default", access=access)
        # Second call gives no access at all — it should still work, using
        # what the first call stored.
        return await auth_service.provision_crew_member(user, "toolshed")

    updated = _run(go)
    assert {c.name for c in updated.crew} == {"default", "toolshed"}


def test_discover_crew_accepts_access_inline_on_a_fresh_account(tmp_path, monkeypatch):
    settings.db_path = str(tmp_path / "t.db")
    _install_fake_discovery(monkeypatch, {"default": {"port": 8642, "api_key": "k"}})

    async def go():
        user = await _new_user()
        access = SSHConnection(
            host="hermes-host.lan", port=22, username="david", private_key="k"
        )
        return await auth_service.discover_crew(user, access=access)

    updated = _run(go)
    assert updated.ssh.host == "hermes-host.lan"
    assert updated.crew[0].name == "default"


# ── where a crew member lives: its own port, or a path on the shared gateway ─────────────────
def test_a_crew_members_address_includes_its_path(tmp_path, monkeypatch):
    settings.db_path = str(tmp_path / "t.db")
    seen = []
    monkeypatch.setattr(auth_service.agent_runtime, "backend_for", lambda base_url, key: seen.append((base_url, key)) or object())

    async def go():
        user = await _new_user()
        user = await auth_service.connect_agent(user, "http://box", 8642, "default-key", "default")
        user = await auth_service.add_crew_member(user, "toolshed", 8642, "ts-key", path="/p/toolshed")
        user = await auth_service.add_crew_member(user, "legacy", 8650, "legacy-key")
        for name in ("default", "toolshed", "legacy"):
            auth_service.backend_for_crew(user, auth_service.find_crew_member(user, name))

    _run(go)
    assert seen == [
        ("http://box:8642", "default-key"),
        ("http://box:8642/p/toolshed", "ts-key"),
        ("http://box:8650", "legacy-key"),
    ]


def test_a_crew_path_is_validated_and_normalized():
    from gaca_api.models.user import CrewMember
    assert CrewMember(name="a", port=1, api_key="k", path="/p/toolshed/").path == "/p/toolshed"
    assert CrewMember(name="a", port=1, api_key="k").path == ""
    for bad in ("p/toolshed", "/p/../x", "/p/a b", "http://evil", "/p/x?y=1", "//x"):
        with pytest.raises(ValueError):
            CrewMember(name="a", port=1, api_key="k", path=bad)


def test_updating_a_crew_member_keeps_its_path_unless_told_otherwise(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        user = await _new_user()
        user = await auth_service.add_crew_member(user, "toolshed", 8642, "k", path="/p/toolshed")
        kept = await auth_service.update_crew_member(user, "toolshed", 8642, "k2")
        changed = await auth_service.update_crew_member(kept, "toolshed", 8650, "k3", path="")
        return kept.crew[0], changed.crew[0]

    kept, changed = _run(go)
    assert (kept.path, kept.api_key) == ("/p/toolshed", "k2")  # an edit that doesn't mention path must not reset it
    assert (changed.path, changed.port) == ("", 8650)


def test_discover_crew_follows_a_member_that_moved_to_the_shared_gateway(tmp_path, monkeypatch):
    """The real incident (2026-10-03): Hermes moved toolshed from its own port to /p/toolshed on the
    default gateway. Same key, new address -> follow it; different key -> leave it alone."""
    settings.db_path = str(tmp_path / "t.db")
    _install_fake_discovery(monkeypatch, {
        "default": {"port": 8642, "api_key": "default-key", "path": ""},
        "toolshed": {"port": 8642, "api_key": "ts-key", "path": "/p/toolshed"},
        "trading": {"port": 8642, "api_key": "NEW-key", "path": "/p/trading"},
        "david": {"port": 8642, "api_key": "david-key", "path": "/p/david"},
    })

    async def go():
        user = await _connected_user()
        user = await auth_service.add_crew_member(user, "default", 8642, "default-key")
        user = await auth_service.add_crew_member(user, "toolshed", 8643, "ts-key")
        user = await auth_service.add_crew_member(user, "trading", 8644, "old-key")
        return await auth_service.discover_crew(user)

    updated = _run(go)
    by_name = {c.name: c for c in updated.crew}
    assert (by_name["toolshed"].port, by_name["toolshed"].path, by_name["toolshed"].api_key) == (8642, "/p/toolshed", "ts-key")
    assert (by_name["trading"].port, by_name["trading"].path, by_name["trading"].api_key) == (8644, "", "old-key")  # key differs
    assert (by_name["default"].port, by_name["default"].path) == (8642, "")  # unchanged
    assert (by_name["david"].port, by_name["david"].path) == (8642, "/p/david")  # newly found, with its path


def test_provision_crew_member_records_the_address_the_box_reports(tmp_path, monkeypatch):
    settings.db_path = str(tmp_path / "t.db")
    calls: list = []
    _install_fake_provisioning(monkeypatch, port=8650, calls=calls, address=(8642, "/p/newone"))

    async def go():
        return await auth_service.provision_crew_member(await _connected_user(), "newone")

    member = _run(go).crew[0]
    assert calls[0][1] == 8650  # the candidate port offered for a gateway-per-profile host
    assert (member.port, member.path) == (8642, "/p/newone")  # ...but the shared gateway's address was recorded
