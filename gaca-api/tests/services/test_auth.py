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
