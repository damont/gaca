"""Tests for the push service: token registration, ownership, best-effort send."""
import asyncio

from gaca_api.config import settings
from gaca_api.db import client
from gaca_api.db import push_tokens as push_tokens_db
from gaca_api.services import push as push_service


def test_register_token_is_findable_by_user(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def main():
        await client.connect()
        try:
            await push_service.register_token("user-1", "token-abc", "android")
            return await push_tokens_db.find_by_user("user-1")
        finally:
            await client.close()

    tokens = asyncio.run(main())
    assert len(tokens) == 1
    assert tokens[0].token == "token-abc"
    assert tokens[0].platform == "android"


def test_registering_same_token_again_is_idempotent(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def main():
        await client.connect()
        try:
            await push_service.register_token("user-1", "token-abc", "android")
            await push_service.register_token("user-1", "token-abc", "android")
            return await push_tokens_db.find_by_user("user-1")
        finally:
            await client.close()

    tokens = asyncio.run(main())
    assert len(tokens) == 1


def test_token_reregistered_under_new_user_moves_ownership(tmp_path):
    """The same device signing into a different account should move the
    token, not fan out alerts to whoever registered it first."""
    settings.db_path = str(tmp_path / "t.db")

    async def main():
        await client.connect()
        try:
            await push_service.register_token("user-1", "shared-token", "android")
            await push_service.register_token("user-2", "shared-token", "android")
            return (
                await push_tokens_db.find_by_user("user-1"),
                await push_tokens_db.find_by_user("user-2"),
            )
        finally:
            await client.close()

    user1_tokens, user2_tokens = asyncio.run(main())
    assert user1_tokens == []
    assert len(user2_tokens) == 1
    assert user2_tokens[0].token == "shared-token"


def test_unregister_token_owned_by_caller_deletes_it(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def main():
        await client.connect()
        try:
            await push_service.register_token("user-1", "token-abc", "android")
            await push_service.unregister_token("user-1", "token-abc")
            return await push_tokens_db.find_by_user("user-1")
        finally:
            await client.close()

    tokens = asyncio.run(main())
    assert tokens == []


def test_unregister_token_not_owned_by_caller_is_a_noop(tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def main():
        await client.connect()
        try:
            await push_service.register_token("user-1", "token-abc", "android")
            await push_service.unregister_token("user-2", "token-abc")
            return await push_tokens_db.find_by_user("user-1")
        finally:
            await client.close()

    tokens = asyncio.run(main())
    assert len(tokens) == 1


def test_notify_is_a_noop_when_fcm_is_not_configured(tmp_path):
    """No GACA_FCM_CREDENTIALS_PATH set (the test/dev default) means notify()
    must not raise or block — alert creation and delegation depend on that."""
    settings.db_path = str(tmp_path / "t.db")

    async def main():
        await client.connect()
        try:
            await push_service.register_token("user-1", "token-abc", "android")
            await push_service.notify(
                "user-1", title="Hi", body="Body", alert_id="a1", severity="info"
            )
        finally:
            await client.close()

    asyncio.run(main())  # no exception is the assertion
