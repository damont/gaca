"""Push notification fan-out: Firebase Cloud Messaging, plus APNs for iPhones.

Android (and anything that isn't platform "ios") goes through FCM; iOS tokens
go straight to Apple via services/apns.py. Callers don't care which — they
call notify()/notify_chat() and every device the user registered gets it.

Best-effort everywhere it's called: alert creation and delegation settlement
must never fail or slow down because a push couldn't be sent. Leaving
GACA_FCM_CREDENTIALS_PATH unset disables sending entirely — connect() no-ops,
notify() no-ops — which is the same posture as the app already has without
this file: the Android client's own polling is still the source of truth,
push is purely a "tell me sooner" nicety on top of it. APNs follows the same
rule with its own settings (GACA_APNS_*); either can be on without the other.
"""
from __future__ import annotations

import asyncio

import firebase_admin
from firebase_admin import credentials, messaging

from ..config import settings
from ..db import push_tokens as push_tokens_db
from ..models.push_token import PushTokenDoc
from . import apns

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


async def register_token(
    user_id: str, token: str, platform: str, environment: str | None = None
) -> None:
    await push_tokens_db.upsert(
        PushTokenDoc(user_id=user_id, token=token, platform=platform, environment=environment)
    )


async def unregister_token(user_id: str, token: str) -> None:
    """Drop a token, scoped to its owner — a stray/mistaken id for someone
    else's token silently no-ops rather than letting one account delete
    another's registration."""
    owned = await push_tokens_db.find_by_user(user_id)
    if any(t.token == token for t in owned):
        await push_tokens_db.delete_by_token(token)


def connect() -> None:
    """Initialize the Firebase Admin SDK and the APNs client (startup), each if configured."""
    global _app
    apns.connect()
    if not settings.fcm_credentials_path:
        return
    cred = credentials.Certificate(settings.fcm_credentials_path)
    _app = firebase_admin.initialize_app(cred)


async def close() -> None:
    global _app
    if _app is not None:
        firebase_admin.delete_app(_app)
        _app = None
    await apns.close()


async def _send_to_user(
    user_id: str,
    *,
    title: str,
    body: str,
    data: dict[str, str],
    channel_id: str,
    collapse_id: str,
    thread_id: str,
    time_sensitive: bool = False,
) -> None:
    """Push to every device this user has registered. Best effort, never raises:
    an unregistered/invalid token is deleted so it stops being tried, anything
    else is swallowed — the contract is "tell me sooner," not "delivered".

    `channel_id` only means something to Android; `collapse_id`, `thread_id`
    and `time_sensitive` only to iOS. Both platforms get the same `data`.
    """
    for push_token in await push_tokens_db.find_by_user(user_id):
        if push_token.platform == "ios":
            if not apns.is_configured():
                continue
            result = await apns.send(
                push_token.token,
                push_token.environment,
                title=title,
                body=body,
                collapse_id=collapse_id,
                thread_id=thread_id,
                time_sensitive=time_sensitive,
                custom=data,
            )
            if result is apns.SendResult.DEAD_TOKEN:
                await push_tokens_db.delete_by_token(push_token.token)
            continue
        if _app is None:
            continue
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
        # One notification per alert: a re-push of the same alert replaces it.
        collapse_id=alert_id,
        thread_id="alerts",
        # The iOS counterpart of Android's urgent channel: breaks through Focus.
        time_sensitive=severity == "urgent",
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
        collapse_id=session_id,
        thread_id="chat",
    )
