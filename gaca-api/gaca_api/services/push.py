"""Push notification fan-out via Firebase Cloud Messaging.

Best-effort everywhere it's called: alert creation and delegation settlement
must never fail or slow down because a push couldn't be sent. Leaving
GACA_FCM_CREDENTIALS_PATH unset disables sending entirely — connect() no-ops,
notify() no-ops — which is the same posture as the app already has without
this file: the Android client's own polling is still the source of truth,
push is purely a "tell me sooner" nicety on top of it.
"""
from __future__ import annotations

import asyncio

import firebase_admin
from firebase_admin import credentials, messaging

from ..config import settings
from ..db import push_tokens as push_tokens_db
from ..models.push_token import PushTokenDoc

_app: firebase_admin.App | None = None

# Must match the Android app's channel ids exactly (strings.xml,
# notification_channel_*_id) — this is the one place both repos have to agree
# on a literal string. Setting the channel here (not just relying on the app's
# own onMessageReceived) matters because a notification+data message that
# arrives while the app is backgrounded is displayed by the OS directly,
# without the app ever running its own channel-selection code.
_CHANNEL_BY_SEVERITY = {
    "urgent": "alerts_urgent",
}
_DEFAULT_CHANNEL = "alerts_standard"
CHAT_CHANNEL = "chat_replies"


async def register_token(user_id: str, token: str, platform: str) -> None:
    await push_tokens_db.upsert(PushTokenDoc(user_id=user_id, token=token, platform=platform))


async def unregister_token(user_id: str, token: str) -> None:
    """Drop a token, scoped to its owner — a stray/mistaken id for someone
    else's token silently no-ops rather than letting one account delete
    another's registration."""
    owned = await push_tokens_db.find_by_user(user_id)
    if any(t.token == token for t in owned):
        await push_tokens_db.delete_by_token(token)


def connect() -> None:
    """Initialize the Firebase Admin SDK (startup), if configured."""
    global _app
    if not settings.fcm_credentials_path:
        return
    cred = credentials.Certificate(settings.fcm_credentials_path)
    _app = firebase_admin.initialize_app(cred)


def close() -> None:
    global _app
    if _app is not None:
        firebase_admin.delete_app(_app)
        _app = None


async def aclose() -> None:
    """Shut Firebase down from async code. firebase_admin's cleanup calls asyncio.run() once its
    messaging client exists (after the first push), which raises inside a running event loop, so
    it has to run in a worker thread that has no loop of its own."""
    await asyncio.to_thread(close)


async def _send_to_user(
    user_id: str, *, title: str, body: str, data: dict[str, str], channel_id: str
) -> None:
    """Push to every device this user has registered. Best effort, never raises:
    an unregistered/invalid token is deleted so it stops being tried, anything
    else is swallowed — the contract is "tell me sooner," not "delivered"."""
    if _app is None:
        return
    for push_token in await push_tokens_db.find_by_user(user_id):
        message = messaging.Message(
            token=push_token.token,
            notification=messaging.Notification(title=title, body=body),
            data=data,
            android=messaging.AndroidConfig(
                priority="high",
                notification=messaging.AndroidNotification(channel_id=channel_id),
            ),
        )
        try:
            await asyncio.to_thread(messaging.send, message, app=_app)
        except messaging.UnregisteredError:
            await push_tokens_db.delete_by_token(push_token.token)
        except Exception:
            pass


async def notify(user_id: str, *, title: str, body: str, alert_id: str, severity: str) -> None:
    """Push an alert to this user's devices.

    `alert_id` and `severity` ride along as data-only fields (not part of the
    visible notification) so the Android app can deep-link to the right alert
    and pick a notification channel (urgent vs. everything else) on arrival.
    """
    await _send_to_user(
        user_id,
        title=title,
        body=body,
        data={"alert_id": alert_id, "severity": severity},
        channel_id=_CHANNEL_BY_SEVERITY.get(severity, _DEFAULT_CHANNEL),
    )


async def notify_chat(user_id: str, *, title: str, body: str, session_id: str) -> None:
    """Push "your agent replied" for a normal chat.

    `session_id` rides along as data so the app can open that thread on tap, and
    skip showing the notification when you're already looking at it. Uses its own
    channel so chat can be muted separately from alerts.
    """
    await _send_to_user(
        user_id,
        title=title,
        body=body,
        data={"session_id": session_id, "kind": "chat"},
        channel_id=CHAT_CHANNEL,
    )
