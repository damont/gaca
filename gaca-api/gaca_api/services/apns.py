"""Push to iPhones directly through Apple Push Notification service (APNs).

Why not FCM for iOS too: routing iOS through Firebase means shipping the
Firebase SDK in the iPhone app and uploading the APNs key to Firebase anyway.
Talking to Apple's HTTP/2 provider API ourselves keeps the iOS client free of
a third-party SDK — it hands us its raw APNs device token and that's it.

Auth is token-based (a .p8 signing key from the Apple Developer portal), not a
per-app certificate: one key works for every app in the team and never
expires. Each request carries a short JWT signed with it (ES256); Apple wants
that JWT reused, not minted per request, and refreshed at most once every 20
minutes and at least once an hour — so it's cached and re-minted after ~50.

Same posture as FCM in services/push.py: disabled (every call a no-op) unless
GACA_APNS_KEY_PATH, GACA_APNS_KEY_ID and GACA_APNS_TEAM_ID are all set, and
nothing here ever raises into the caller.
"""
from __future__ import annotations

import time
from enum import Enum
from pathlib import Path

import httpx
import jwt

from ..config import settings

# Which Apple endpoint a token belongs to is fixed by how the app was signed:
# a debug build from Xcode gets sandbox tokens, TestFlight/App Store builds get
# production ones. A token sent to the wrong host comes back BadDeviceToken,
# so the client tells us which it is when it registers.
_HOSTS = {
    "sandbox": "https://api.sandbox.push.apple.com",
    "production": "https://api.push.apple.com",
}
_DEFAULT_ENVIRONMENT = "production"

# Apple rejects a provider token older than an hour (ExpiredProviderToken) and
# throttles one refreshed more often than every 20 minutes (TooManyProviderTokenUpdates).
_JWT_MAX_AGE_SECONDS = 50 * 60

# Apple's way of saying "this token will never work again" — the APNs
# equivalent of FCM's UnregisteredError. 410 is "app uninstalled"; the 400
# reasons are a token from another app or environment, equally hopeless.
_DEAD_TOKEN_REASONS = {"BadDeviceToken", "DeviceTokenNotForTopic"}

_client: httpx.AsyncClient | None = None
_signing_key: str | None = None
_jwt: tuple[str, float] | None = None  # (token, minted_at)


class SendResult(Enum):
    SENT = "sent"
    DEAD_TOKEN = "dead_token"  # caller should delete it
    FAILED = "failed"  # anything else — swallowed, try again next time


def is_configured() -> bool:
    return bool(settings.apns_key_path and settings.apns_key_id and settings.apns_team_id)


def connect() -> None:
    """Open the shared HTTP/2 client (startup), if configured.

    One long-lived client, because APNs is built around a persistent HTTP/2
    connection multiplexing many requests — opening one per push is exactly
    what Apple asks providers not to do.
    """
    global _client
    if not is_configured():
        return
    _client = httpx.AsyncClient(http2=True, timeout=10.0)


async def close() -> None:
    global _client, _signing_key, _jwt
    if _client is not None:
        await _client.aclose()
        _client = None
    _signing_key = None
    _jwt = None


def _provider_token() -> str:
    """The cached provider JWT, minted (or re-minted) when missing or ~50 min old."""
    global _signing_key, _jwt
    now = time.time()
    if _jwt is not None and now - _jwt[1] < _JWT_MAX_AGE_SECONDS:
        return _jwt[0]
    if _signing_key is None:
        _signing_key = Path(settings.apns_key_path).read_text()
    token = jwt.encode(
        {"iss": settings.apns_team_id, "iat": int(now)},
        _signing_key,
        algorithm="ES256",
        headers={"kid": settings.apns_key_id},
    )
    _jwt = (token, now)
    return token


async def send(
    device_token: str,
    environment: str | None,
    *,
    title: str,
    body: str,
    collapse_id: str,
    thread_id: str,
    time_sensitive: bool,
    custom: dict[str, str],
) -> SendResult:
    """Send one alert-style push. Never raises.

    `collapse_id` makes a newer push for the same alert/chat replace the older
    one on the lock screen — the iOS counterpart of Android posting one
    notification per alert/chat id. `custom` keys sit beside `aps` at the top
    level, where the app reads them on tap to deep-link.
    """
    if _client is None:
        return SendResult.FAILED
    host = _HOSTS.get(environment or _DEFAULT_ENVIRONMENT, _HOSTS[_DEFAULT_ENVIRONMENT])
    payload = {
        "aps": {
            "alert": {"title": title, "body": body},
            "sound": "default",
            "thread-id": thread_id,
            "interruption-level": "time-sensitive" if time_sensitive else "active",
        },
        **custom,
    }
    try:
        response = await _client.post(
            f"{host}/3/device/{device_token}",
            json=payload,
            headers={
                "authorization": f"bearer {_provider_token()}",
                "apns-topic": settings.apns_topic,
                "apns-push-type": "alert",
                "apns-priority": "10",
                # Apple caps this at 64 bytes; our ids are 32-char hex.
                "apns-collapse-id": collapse_id[:64],
            },
        )
    except Exception:
        return SendResult.FAILED
    if response.status_code == 200:
        return SendResult.SENT
    if response.status_code == 410:
        return SendResult.DEAD_TOKEN
    if response.status_code == 400:
        try:
            reason = response.json().get("reason")
        except Exception:
            reason = None
        if reason in _DEAD_TOKEN_REASONS:
            return SendResult.DEAD_TOKEN
    return SendResult.FAILED
