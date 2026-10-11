"""Tests for the push service: token registration, ownership, best-effort send
over FCM (Android) and APNs (iOS)."""
import asyncio
import json

import httpx
import jwt
import pytest
from pydantic import ValidationError
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from gaca_api.config import settings
from gaca_api.db import client
from gaca_api.db import push_tokens as push_tokens_db
from gaca_api.schemas.push import PushTokenRegister
from gaca_api.services import apns
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


def test_notify_chat_sends_session_id_on_the_chat_channel(tmp_path, monkeypatch):
    settings.db_path = str(tmp_path / "t.db")
    sent = []
    monkeypatch.setattr(push_service, "_app", object())
    monkeypatch.setattr(push_service.messaging, "send", lambda message, app=None: sent.append(message))

    async def main():
        await client.connect()
        try:
            await push_service.register_token("user-1", "tok-1", "android")
            await push_service.register_token("user-2", "tok-2", "android")
            await push_service.notify_chat("user-1", title="Hermes", body="Hi!", session_id="s1")
        finally:
            await client.close()

    asyncio.run(main())
    assert len(sent) == 1  # only user-1's device
    msg = sent[0]
    assert msg.token == "tok-1"
    assert msg.data == {"session_id": "s1", "kind": "chat"}
    assert msg.android.notification.channel_id == "chat_replies"
    assert (msg.notification.title, msg.notification.body) == ("Hermes", "Hi!")


# --- APNs (iOS) ---------------------------------------------------------------

class _Captured(list):
    """The requests APNs "received", plus knobs for what it answers."""


@pytest.fixture
def apns_requests(tmp_path, monkeypatch):
    """Configure APNs with a throwaway P-256 key and route its HTTP client
    through a mock transport — no network. Yields the list of captured
    requests; update its `.reply` to change what Apple "answers"."""
    key = ec.generate_private_key(ec.SECP256R1())
    key_path = tmp_path / "AuthKey_TEST.p8"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    monkeypatch.setattr(settings, "apns_key_path", str(key_path))
    monkeypatch.setattr(settings, "apns_key_id", "KEYID12345")
    monkeypatch.setattr(settings, "apns_team_id", "TEAMID6789")
    monkeypatch.setattr(apns, "_signing_key", None)
    monkeypatch.setattr(apns, "_jwt", None)

    captured = _Captured()
    reply = {"status": 200, "reason": None}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if reply["reason"]:
            return httpx.Response(reply["status"], json={"reason": reply["reason"]})
        return httpx.Response(reply["status"])

    monkeypatch.setattr(apns, "_client", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    captured.reply = reply
    captured.public_key = key.public_key()
    return captured


def _run(tmp_path, *calls):
    settings.db_path = str(tmp_path / "t.db")

    async def main():
        await client.connect()
        try:
            for call in calls:
                await call()
            return await push_tokens_db.find_by_user("user-1")
        finally:
            await client.close()

    return asyncio.run(main())


def test_ios_alert_goes_to_apns_with_headers_and_payload(tmp_path, apns_requests):
    _run(
        tmp_path,
        lambda: push_service.register_token("user-1", "ios-tok", "ios", "production"),
        lambda: push_service.notify(
            "user-1", title="Door open", body="Garage", alert_id="a1", severity="urgent"
        ),
    )
    assert len(apns_requests) == 1
    req = apns_requests[0]
    assert str(req.url) == "https://api.push.apple.com/3/device/ios-tok"
    assert req.headers["apns-topic"] == "dev.monts.gaca"
    assert req.headers["apns-push-type"] == "alert"
    assert req.headers["apns-priority"] == "10"
    assert req.headers["apns-collapse-id"] == "a1"
    scheme, token = req.headers["authorization"].split(" ")
    assert scheme == "bearer"
    assert jwt.get_unverified_header(token)["kid"] == "KEYID12345"
    claims = jwt.decode(token, apns_requests.public_key, algorithms=["ES256"])
    assert claims["iss"] == "TEAMID6789" and "iat" in claims
    assert json.loads(req.content) == {
        "aps": {
            "alert": {"title": "Door open", "body": "Garage"},
            "sound": "default",
            "thread-id": "alerts",
            "interruption-level": "time-sensitive",
        },
        "alert_id": "a1",
        "severity": "urgent",
    }


def test_ios_chat_push_collapses_on_session_and_is_not_time_sensitive(tmp_path, apns_requests):
    _run(
        tmp_path,
        lambda: push_service.register_token("user-1", "ios-tok", "ios"),
        lambda: push_service.notify_chat("user-1", title="Hermes", body="Hi!", session_id="s1"),
    )
    req = apns_requests[0]
    assert req.headers["apns-collapse-id"] == "s1"
    body = json.loads(req.content)
    assert body["aps"]["thread-id"] == "chat"
    assert body["aps"]["interruption-level"] == "active"
    assert (body["session_id"], body["kind"]) == ("s1", "chat")


def test_ios_token_environment_picks_the_apns_host(tmp_path, apns_requests):
    """Sandbox tokens (Xcode debug builds) must go to the sandbox host; a token
    registered without an environment is treated as production."""
    _run(
        tmp_path,
        lambda: push_service.register_token("user-1", "dev-tok", "ios", "sandbox"),
        lambda: push_service.register_token("user-1", "old-tok", "ios"),
        lambda: push_service.notify("user-1", title="t", body="b", alert_id="a1", severity="info"),
    )
    hosts = {req.url.path.rsplit("/", 1)[1]: req.url.host for req in apns_requests}
    assert hosts == {"dev-tok": "api.sandbox.push.apple.com", "old-tok": "api.push.apple.com"}


def test_provider_jwt_is_reused_then_refreshed_after_50_minutes(tmp_path, apns_requests, monkeypatch):
    now = [1_000_000.0]
    monkeypatch.setattr(apns.time, "time", lambda: now[0])
    first = apns._provider_token()
    now[0] += 49 * 60
    assert apns._provider_token() == first
    now[0] += 2 * 60
    assert apns._provider_token() != first


@pytest.mark.parametrize(
    ("status", "reason", "deleted"),
    [
        (410, "Unregistered", True),
        (400, "BadDeviceToken", True),
        (400, "DeviceTokenNotForTopic", True),
        (400, "PayloadTooLarge", False),
        (500, "InternalServerError", False),
    ],
)
def test_dead_apns_tokens_are_deleted_and_other_errors_swallowed(
    tmp_path, apns_requests, status, reason, deleted
):
    apns_requests.reply.update(status=status, reason=reason)
    tokens = _run(
        tmp_path,
        lambda: push_service.register_token("user-1", "ios-tok", "ios"),
        lambda: push_service.notify("user-1", title="t", body="b", alert_id="a1", severity="info"),
    )
    assert (tokens == []) is deleted


def test_android_still_fcm_and_mixed_user_gets_both(tmp_path, apns_requests, monkeypatch):
    sent = []
    monkeypatch.setattr(push_service, "_app", object())
    monkeypatch.setattr(push_service.messaging, "send", lambda message, app=None: sent.append(message))
    _run(
        tmp_path,
        lambda: push_service.register_token("user-1", "android-tok", "android"),
        lambda: push_service.register_token("user-1", "ios-tok", "ios"),
        lambda: push_service.notify("user-1", title="t", body="b", alert_id="a1", severity="info"),
    )
    assert [m.token for m in sent] == ["android-tok"]
    assert sent[0].data == {"alert_id": "a1", "severity": "info"}
    assert sent[0].android.notification.channel_id == "alerts_standard"
    assert [r.url.path for r in apns_requests] == ["/3/device/ios-tok"]


def test_ios_token_is_a_noop_when_apns_is_not_configured(tmp_path, monkeypatch):
    """No GACA_APNS_* set (the default): an ios token is skipped — not sent to
    FCM, not deleted, nothing raised."""
    sent = []
    monkeypatch.setattr(push_service, "_app", object())
    monkeypatch.setattr(push_service.messaging, "send", lambda message, app=None: sent.append(message))
    monkeypatch.setattr(apns, "_client", None)
    tokens = _run(
        tmp_path,
        lambda: push_service.register_token("user-1", "ios-tok", "ios"),
        lambda: push_service.notify("user-1", title="t", body="b", alert_id="a1", severity="info"),
    )
    assert sent == []
    assert [t.token for t in tokens] == ["ios-tok"]


def test_register_rejects_an_unknown_apns_environment():
    assert PushTokenRegister(token="t", platform="ios", environment="sandbox").environment == "sandbox"
    assert PushTokenRegister(token="t", platform="ios").environment is None
    with pytest.raises(ValidationError):
        PushTokenRegister(token="t", platform="ios", environment="staging")
