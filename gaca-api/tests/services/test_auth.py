"""Tests for the auth service: create a user, find one by email."""
import asyncio
import sqlite3
from contextlib import asynccontextmanager

import asyncssh
import pytest

from gaca_api.config import settings
from gaca_api.db import client
from gaca_api.db import users as users_db
from gaca_api.models.user import UserDoc
from gaca_api.provisioning.ssh import SSHError
from gaca_api.services import auth as auth_service
from gaca_api.services.exceptions import CrewNotConfigured, InvalidRequest, NotFound
from gaca_api.utils.auth import hash_password, verify_password


class _AcceptingServer(asyncssh.SSHServer):
    """Minimal SSH server accepting one client key — see
    tests/provisioning/test_ssh.py for the fuller version this mirrors."""

    def __init__(self, authorized_key: asyncssh.SSHKey) -> None:
        self._authorized_key = authorized_key

    def begin_auth(self, username: str) -> bool:
        return True

    def public_key_auth_supported(self) -> bool:
        return True

    def validate_public_key(self, username: str, key: asyncssh.SSHKey) -> bool:
        return key == self._authorized_key


async def _echo_ok(process: asyncssh.SSHServerProcess) -> None:
    process.stdout.write("ok\n")
    process.exit(0)


@asynccontextmanager
async def _local_ssh_server():
    server_key = asyncssh.generate_private_key("ssh-ed25519")
    client_key = asyncssh.generate_private_key("ssh-ed25519")
    server = await asyncssh.create_server(
        lambda: _AcceptingServer(client_key.convert_to_public()),
        host="127.0.0.1",
        port=0,
        server_host_keys=[server_key],
        process_factory=_echo_ok,
    )
    try:
        yield server.sockets[0].getsockname()[1], client_key
    finally:
        server.close()
        await server.wait_closed()


def _run(factory):
    async def main():
        await client.connect()
        try:
            return await factory()
        finally:
            await client.close()

    return asyncio.run(main())


def test_create_user_persists_and_is_findable(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def main():
        await client.connect()
        try:
            created = await auth_service.create_user(
                name="David", email="david@example.com", password="hunter2"
            )
            found = await auth_service.find_by_email("david@example.com")
            return created, found
        finally:
            await client.close()

    created, found = asyncio.run(main())
    assert found is not None
    assert found.id == created.id
    assert found.display_name == "David"
    assert found.is_active is True


def test_password_is_hashed_never_stored_plaintext(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def main():
        await client.connect()
        try:
            await auth_service.create_user(
                name="David", email="david@example.com", password="hunter2"
            )
            found = await auth_service.find_by_email("david@example.com")
            # Read the raw stored JSON too — the model could hide a field, the
            # row cannot.
            async with client.connection().execute(
                "SELECT doc FROM users WHERE email = ?", ("david@example.com",)
            ) as cursor:
                row = await cursor.fetchone()
            return found, row["doc"]
        finally:
            await client.close()

    found, raw = asyncio.run(main())
    assert "hunter2" not in raw
    assert found.hashed_password != "hunter2"
    assert verify_password("hunter2", found.hashed_password)


def test_find_by_email_unknown_returns_none(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def main():
        await client.connect()
        try:
            return await auth_service.find_by_email("nobody@example.com")
        finally:
            await client.close()

    assert asyncio.run(main()) is None


# ── Google sign-in ──────────────────────────────────────────────────

def test_google_login_creates_a_passwordless_account(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def main():
        await client.connect()
        try:
            return await auth_service.upsert_google_user(
                google_sub="google-123", email="david@example.com", name="David"
            )
        finally:
            await client.close()

    user = asyncio.run(main())
    assert user.google_sub == "google-123"
    assert user.hashed_password is None  # can only ever sign in via Google
    assert user.email_verified is True


def test_google_login_is_idempotent_for_a_known_sub(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def main():
        await client.connect()
        try:
            first = await auth_service.upsert_google_user(
                google_sub="google-123", email="david@example.com", name="David"
            )
            second = await auth_service.upsert_google_user(
                google_sub="google-123", email="david@example.com", name="David"
            )
            return first, second
        finally:
            await client.close()

    first, second = asyncio.run(main())
    assert first.id == second.id


def test_google_links_onto_an_existing_password_account(tmp_path):
    """Same person, one row — not a second account they have to keep straight."""
    settings.db_path = str(tmp_path / "t.db")

    async def main():
        await client.connect()
        try:
            original = await auth_service.create_user(
                name="David", email="david@example.com", password="hunter2"
            )
            linked = await auth_service.upsert_google_user(
                google_sub="google-123", email="david@example.com", name="David"
            )
            return original, linked, await auth_service.find_by_email("david@example.com")
        finally:
            await client.close()

    original, linked, reloaded = asyncio.run(main())
    assert linked.id == original.id
    assert reloaded.google_sub == "google-123"
    # Linking must not cost them their password — both routes still work.
    assert verify_password("hunter2", reloaded.hashed_password)


def test_a_google_only_account_cannot_be_password_logged_in(tmp_path):
    """The login route's `not user.hashed_password` guard is what stops this.

    Without it, verify_password would be handed None and the failure mode would
    be a 500 rather than a clean rejection.
    """
    settings.db_path = str(tmp_path / "t.db")

    async def main():
        await client.connect()
        try:
            await auth_service.upsert_google_user(
                google_sub="google-123", email="david@example.com", name="David"
            )
            return await auth_service.find_by_email("david@example.com")
        finally:
            await client.close()

    user = asyncio.run(main())
    assert not user.hashed_password


def test_google_sub_is_unique_but_absent_subs_do_not_collide(tmp_path):
    """The partial index is why two password-only users can coexist.

    A plain UNIQUE index would treat their NULL google_sub values as colliding
    in some engines; `WHERE google_sub IS NOT NULL` scopes it to rows that
    actually have one.
    """
    settings.db_path = str(tmp_path / "t.db")

    async def main():
        await client.connect()
        try:
            await auth_service.create_user(
                name="A", email="a@example.com", password="pw"
            )
            await auth_service.create_user(
                name="B", email="b@example.com", password="pw"
            )  # second NULL google_sub — must not raise

            await auth_service.upsert_google_user(
                google_sub="google-123", email="c@example.com", name="C"
            )
            with pytest.raises(sqlite3.IntegrityError):
                await users_db.insert(
                    UserDoc(
                        email="d@example.com",
                        display_name="D",
                        google_sub="google-123",  # duplicate sub
                    )
                )
        finally:
            await client.close()

    asyncio.run(main())


def test_missing_columns_are_added_to_a_pre_existing_database(tmp_path):
    """A database created before google_sub existed still gets the column.

    CREATE TABLE IF NOT EXISTS does nothing when the table is already there, so
    without the migration the schema would be right on a fresh machine and
    quietly wrong on one that had been running for a while.
    """
    db = tmp_path / "old.db"
    settings.db_path = str(db)

    legacy = sqlite3.connect(db)
    legacy.execute(
        "CREATE TABLE users (id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE, "
        "created_at TEXT NOT NULL, doc TEXT NOT NULL)"
    )
    legacy.commit()
    legacy.close()

    async def main():
        await client.connect()
        try:
            async with client.connection().execute("PRAGMA table_info(users)") as cur:
                columns = {row["name"] for row in await cur.fetchall()}
            # and the table is usable afterwards
            await auth_service.upsert_google_user(
                google_sub="google-123", email="david@example.com", name="David"
            )
            return columns
        finally:
            await client.close()

    assert "google_sub" in asyncio.run(main())


def test_duplicate_email_rejected_by_the_database(tmp_path):
    """The route checks for a taken email first, but two requests can race it.

    The UNIQUE index on users.email is the backstop that makes a duplicate
    impossible rather than merely unlikely.
    """
    settings.db_path = str(tmp_path / "t.db")

    async def main():
        await client.connect()
        try:
            await auth_service.create_user(
                name="David", email="david@example.com", password="hunter2"
            )
            with pytest.raises(sqlite3.IntegrityError):
                await users_db.insert(
                    UserDoc(
                        email="david@example.com",
                        display_name="Impostor",
                        hashed_password=hash_password("other"),
                    )
                )
        finally:
            await client.close()

    asyncio.run(main())


# ── per-user crew config ────────────────────────────────────────────

async def _new_user() -> UserDoc:
    return await auth_service.create_user(
        name="David", email="david@example.com", password="hunter2"
    )


def test_set_agent_host_persists(tmp_path):
    """Moving an already-connected agent to a new host — not first-time
    setup, that's connect_agent below."""
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        user = await _new_user()
        assert user.agent_host is None
        updated = await auth_service.set_agent_host(user, "http://david-claw.lan")
        reloaded = await auth_service.find_by_id(user.id)
        return updated, reloaded

    updated, reloaded = _run(go)
    assert updated.agent_host == "http://david-claw.lan"
    # persisted, not just returned
    assert reloaded.agent_host == "http://david-claw.lan"


def test_connect_agent_sets_host_and_creates_the_first_crew_member(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        user = await _new_user()
        updated = await auth_service.connect_agent(
            user, "http://david-claw.lan", 8642, "the-key"
        )
        reloaded = await auth_service.find_by_id(user.id)
        return updated, reloaded

    updated, reloaded = _run(go)
    for user in (updated, reloaded):
        assert user.agent_host == "http://david-claw.lan"
        assert [c.name for c in user.crew] == ["default"]
        assert user.crew[0].port == 8642
        assert user.crew[0].api_key == "the-key"
        assert user.default_crew == "default"


def test_connect_agent_honors_a_custom_first_crew_member_name(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        user = await _new_user()
        return await auth_service.connect_agent(
            user, "http://david-claw.lan", 8642, "the-key", name="toolshed"
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
        with pytest.raises(CrewNotConfigured):
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
        with pytest.raises(CrewNotConfigured):
            auth_service.resolve_crew_for_delegate(user, None, None)

    _run(go)


# ── SSH provisioning ─────────────────────────────────────────────────

def test_set_ssh_connection_persists(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        user = await _new_user()
        assert user.ssh is None
        updated = await auth_service.set_ssh_connection(
            user, "david-claw.lan", 22, "david", "-----BEGIN KEY-----\nfake\n"
        )
        reloaded = await auth_service.find_by_id(user.id)
        return updated, reloaded

    updated, reloaded = _run(go)
    for user in (updated, reloaded):
        assert user.ssh.host == "david-claw.lan"
        assert user.ssh.port == 22
        assert user.ssh.username == "david"


def test_test_ssh_connection_raises_crew_not_configured_when_unset(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        user = await _new_user()
        with pytest.raises(CrewNotConfigured):
            await auth_service.test_ssh_connection(user)

    _run(go)


def test_test_ssh_connection_succeeds_against_a_real_server(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        async with _local_ssh_server() as (port, client_key):
            user = await _new_user()
            user = await auth_service.set_ssh_connection(
                user,
                "127.0.0.1",
                port,
                "david",
                client_key.export_private_key().decode(),
            )
            return await auth_service.test_ssh_connection(user)

    assert _run(go) == "ok\n"


def test_test_ssh_connection_raises_ssh_error_on_bad_credentials(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        user = await _new_user()
        # Nothing listens on port 1 in a test sandbox — a fast, reliable failure.
        user = await auth_service.set_ssh_connection(
            user,
            "127.0.0.1",
            1,
            "david",
            asyncssh.generate_private_key("ssh-ed25519").export_private_key().decode(),
        )
        await auth_service.test_ssh_connection(user)

    with pytest.raises(SSHError):
        _run(go)


# ── discovery ────────────────────────────────────────────────────────
# Unit-level here (discover_profiles itself is monkeypatched) — the real
# script is already proven end-to-end against a fake filesystem in
# tests/provisioning/test_ssh.py; these tests are about the merge logic:
# what discover_crew does with what discovery finds.

def _install_fake_discovery(monkeypatch, found: dict):
    async def fake_discover_profiles(conn):
        return found

    monkeypatch.setattr(
        auth_service.ssh_provisioning, "discover_profiles", fake_discover_profiles
    )


async def _connected_user() -> UserDoc:
    user = await _new_user()
    return await auth_service.set_ssh_connection(user, "david-claw", 22, "david", "k")


def test_discover_crew_raises_crew_not_configured_without_ssh(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def go():
        user = await _new_user()
        with pytest.raises(CrewNotConfigured):
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
    assert updated.agent_host == "http://david-claw"


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
