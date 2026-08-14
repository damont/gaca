"""Tests for the auth service: create a user, find one by email."""
import asyncio
import sqlite3

import pytest

from gaca_api.config import settings
from gaca_api.db import client
from gaca_api.db import users as users_db
from gaca_api.models.user import UserDoc
from gaca_api.services import auth as auth_service
from gaca_api.utils.auth import hash_password, verify_password


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
