"""The link between this server and one GACA Cloud account: linking (as a CLIENT of the cloud's
device-code API), the stored token, and `call`, the single door every cloud-bound request
in the other cloud_* services goes through.

The cloud account is a separate identity. This server stores only a revocable token for it
(`gsrv_...`, Fernet-encrypted: services/cloud_crypto.py) and never hands it to anyone: not to the
apps, not to Hermes, not in a response, not in an error, not in a log.

Linking (RFC 8628 style):
  start  -> POST {cloud}/auth/device/start {server_name, scope}; we keep the secret `device_code`
            (encrypted) and give the app only the `user_code` and the page to open.
  poll   -> POST {cloud}/auth/device/token {device_code}; until the user approves it answers
            "authorization_pending". On success we store the token and the account.

Why a poll ENDPOINT and not a background task: the app is already on screen waiting for the
user to finish in the browser, so it is the natural clock; there is no task to start, supervise,
restart or leak; a server restart in the middle loses nothing (the pending link is in the
database); `interval` and `slow_down` are honoured per call; and tests drive it one step at a
time with no timing.
"""
from __future__ import annotations

import json
import logging
import socket
from datetime import timedelta
from typing import Any

import httpx

from ..config import settings
from ..db import cloud as db
from ..models.base import utcnow
from ..models.cloud import CloudLink
from . import cloud_client, cloud_crypto
from .exceptions import CloudError, CloudNotLinked, CloudReply, CloudUnavailable

logger = logging.getLogger(__name__)

SCOPES = ("read", "manage")
# Headers of the cloud's answer that are handed on to the app (never Set-Cookie, never anything
# that could name the cloud).
PASS_RESPONSE_HEADERS = ("content-type", "etag", "last-modified", "retry-after", "x-request-id")
_SLOW_DOWN_STEP = 5


# ── helpers shared by every cloud call ───────────────────────────────────────
def _json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return None


def reply_from(resp: httpx.Response) -> CloudReply:
    """The cloud's answer as something to hand to the app. For an error the body is the cloud's
    own `{code, message, detail}`, with `detail` filled from `message` when absent so clients that
    read FastAPI-style `detail` keep working."""
    body = resp.content
    headers = {k: v for k, v in resp.headers.items() if k.lower() in PASS_RESPONSE_HEADERS}
    if resp.status_code >= 400:
        data = _json(resp)
        if isinstance(data, dict) and "detail" not in data and isinstance(data.get("message"), str):
            body = json.dumps({**data, "detail": data["message"]}).encode()
            headers["content-type"] = "application/json"
    return CloudReply(resp.status_code, body, headers)


async def mark_invalid(user_id: str) -> None:
    link = await db.get_link(user_id)
    if link and link.status == "linked":
        await db.save_link(link.model_copy(update={"status": "invalid"}))


async def check(user_id: str, resp: httpx.Response) -> httpx.Response:
    """Decide what an answer from the cloud means to THIS server's caller.

    2xx is success. 304 is an ETag hit: an answer, handed on. 401 means the cloud no longer accepts
    our token: that is about the link, NOT about the app's own login, so it is never passed on as
    401 (the app would think it had been signed out). Redirects and 5xx are the cloud being
    unavailable (a 503 with a cloud error code is the cloud's own "retry", so that is handed on).
    Everything else (403/404/409/410/413/422/423/429 ...) is the cloud's decision and is handed on as it is.
    """
    status = resp.status_code
    if 200 <= status < 300:
        return resp
    if status == 304:
        raise reply_from(resp)
    if status == 401:
        await mark_invalid(user_id)
        raise CloudError(
            "The cloud no longer accepts this server's link. Link it again in Settings.",
            status=502, code="cloud_link_invalid",
        )
    if 300 <= status < 400:
        raise CloudUnavailable()
    if status >= 500:
        data = _json(resp)
        if status == 503 and isinstance(data, dict) and isinstance(data.get("code"), str):
            raise reply_from(resp)
        raise CloudUnavailable()
    raise reply_from(resp)


async def require_link(user_id: str) -> CloudLink:
    link = await db.get_link(user_id)
    if link is None or link.status == "pending" or not link.token_enc:
        raise CloudNotLinked()
    if link.status == "invalid":
        raise CloudError(
            "The cloud no longer accepts this server's link. Link it again in Settings.",
            status=502, code="cloud_link_invalid",
        )
    return link


async def call(
    user_id: str,
    method: str,
    path: str,
    *,
    params: dict | None = None,
    content: bytes | None = None,
    headers: dict[str, str] | None = None,
    request_id: str | None = None,
    timeout: float | None = None,
) -> httpx.Response:
    """One authenticated call to the cloud as this user's linked server. Returns the response
    on success; raises CloudReply / CloudError / CloudUnavailable otherwise (see `check`)."""
    link = await require_link(user_id)
    try:
        token = cloud_crypto.decrypt(link.token_enc)
    except cloud_crypto.CryptoError:
        raise CloudError(
            "This server cannot read its stored cloud link (its key changed). Unlink and link again.",
            status=500, code="cloud_key_unreadable",
        ) from None
    client = cloud_client.client_for(link.cloud_url)
    resp = await client.request(
        method, path, token=token, params=params, content=content, headers=headers,
        request_id=request_id, timeout=timeout,
    )
    return await check(user_id, resp)


async def call_json(user_id: str, method: str, path: str, *, body: Any = None, params: dict | None = None,
                    request_id: str | None = None) -> Any:
    resp = await call(
        user_id, method, path, params=params, request_id=request_id,
        content=None if body is None else json.dumps(body).encode(),
        headers=None if body is None else {"content-type": "application/json"},
    )
    return None if not resp.content else _json(resp)


# ── status ───────────────────────────────────────────────────────────────────
async def status(user_id: str) -> dict[str, Any]:
    link = await db.get_link(user_id)
    if link is None:
        return {"linked": False, "state": "none"}
    if link.status == "pending":
        return {"linked": False, "state": "pending", "scope": link.scope}
    return {
        "linked": link.status == "linked", "state": link.status, "handle": link.handle,
        "account_id": link.account_id, "linked_at": link.linked_at, "scope": link.scope,
    }


# ── linking ──────────────────────────────────────────────────────────────────
def _server_name() -> str:
    name = settings.cloud_server_name.strip() or socket.gethostname() or "gaca-server"
    return name[:64]


async def start(user_id: str, scope: str) -> dict[str, Any]:
    if scope not in SCOPES:
        raise CloudError("scope must be 'read' or 'manage'.", status=422, code="request_invalid")
    existing = await db.get_link(user_id)
    if existing and existing.status == "linked":
        raise CloudError("This server is already linked. Unlink it first to link another account.",
                         status=409, code="already_linked")
    # The cloud address is taken from configuration, here, and from nowhere else.
    base_url = settings.cloud_url
    client = cloud_client.client_for(base_url)
    name = _server_name()
    resp = await client.request(
        "POST", "/auth/device/start", content=json.dumps({"server_name": name, "scope": scope}).encode(),
        headers={"content-type": "application/json"},
    )
    if resp.status_code != 200:
        if 400 <= resp.status_code < 500 and resp.status_code != 401:
            raise reply_from(resp)
        raise CloudUnavailable()
    data = _json(resp)
    try:
        device_code, user_code = data["device_code"], data["user_code"]
        verification_uri, expires_in, interval = data["verification_uri"], int(data["expires_in"]), int(data["interval"])
    except (TypeError, KeyError, ValueError):
        raise CloudUnavailable("The cloud answered in a way this server did not understand.") from None
    now = utcnow()
    await db.save_link(CloudLink(
        user_id=user_id, cloud_url=cloud_client.validate_base_url(base_url), status="pending", scope=scope,
        server_name=name, pending_device_enc=cloud_crypto.encrypt(device_code),
        pending_expires_at=now + timedelta(seconds=expires_in), pending_interval=interval,
        pending_next_poll=now + timedelta(seconds=interval), created_at=now,
    ))
    return {"user_code": user_code, "verification_uri": verification_uri, "expires_in": expires_in, "interval": interval}


async def poll(user_id: str) -> dict[str, Any]:
    """One step of the device-code exchange. Answers `{status, ...}`: pending | linked | expired |
    denied. Called too soon it answers pending without bothering the cloud."""
    link = await db.get_link(user_id)
    if link is None or link.status != "pending" or not link.pending_device_enc:
        if link and link.status == "linked":
            return {"status": "linked", **{k: v for k, v in (await status(user_id)).items() if k != "linked"}}
        raise CloudError("No link is in progress. Start one first.", status=409, code="no_link_in_progress")
    now = utcnow()
    interval = link.pending_interval or 5
    if link.pending_expires_at and now >= link.pending_expires_at:
        await db.delete_link(user_id)
        return {"status": "expired"}
    if link.pending_next_poll and now < link.pending_next_poll:
        return {"status": "pending", "interval": interval,
                "retry_in": max(1, int((link.pending_next_poll - now).total_seconds() + 0.999))}
    try:
        device_code = cloud_crypto.decrypt(link.pending_device_enc)
    except cloud_crypto.CryptoError:
        await db.delete_link(user_id)
        raise CloudError("The link in progress cannot be read any more. Start again.", status=409,
                         code="no_link_in_progress") from None
    resp = await cloud_client.client_for(link.cloud_url).request(
        "POST", "/auth/device/token", content=json.dumps({"device_code": device_code}).encode(),
        headers={"content-type": "application/json"},
    )
    data = _json(resp)
    code = data.get("code") if isinstance(data, dict) else None
    if resp.status_code == 200:
        return await _finish(link, data)
    if resp.status_code == 400 and code == "authorization_pending":
        await db.save_link(link.model_copy(update={"pending_next_poll": now + timedelta(seconds=interval)}))
        return {"status": "pending", "interval": interval}
    if resp.status_code == 400 and code == "slow_down":
        interval += _SLOW_DOWN_STEP
        await db.save_link(link.model_copy(update={
            "pending_interval": interval, "pending_next_poll": now + timedelta(seconds=interval)}))
        return {"status": "pending", "interval": interval}
    if resp.status_code == 400 and code == "expired_token":
        await db.delete_link(user_id)
        return {"status": "expired"}
    if resp.status_code == 400 and code == "access_denied":
        await db.delete_link(user_id)
        return {"status": "denied"}
    if resp.status_code == 429:
        return {"status": "pending", "interval": interval + _SLOW_DOWN_STEP}
    raise CloudUnavailable()


async def _finish(link: CloudLink, data: Any) -> dict[str, Any]:
    try:
        token = data["server_token"]
        account = data["account"]
        server = data.get("server") or {}
        account_id, handle = str(account["id"]), str(account["handle"])
        if not isinstance(token, str) or not token:
            raise ValueError
    except (TypeError, KeyError, ValueError):
        raise CloudUnavailable("The cloud answered in a way this server did not understand.") from None
    now = utcnow()
    # A different account than before: its pointers are not this account's gacabits.
    await db.delete_pointers(link.user_id)
    await db.save_link(link.model_copy(update={
        "status": "linked", "token_enc": cloud_crypto.encrypt(token), "account_id": account_id,
        "handle": handle, "server_id": server.get("id"), "scope": server.get("scope") or link.scope,
        "linked_at": now, "pending_device_enc": None, "pending_expires_at": None,
        "pending_interval": None, "pending_next_poll": None,
    }))
    from . import cloud_list

    cloud_list.forget(link.user_id)
    return {"status": "linked", **{k: v for k, v in (await status(link.user_id)).items() if k != "linked"}}


# ── unlinking ────────────────────────────────────────────────────────────────
async def unlink(user_id: str) -> dict[str, Any]:
    """Revoke the token on the cloud, then forget the link here. If the cloud cannot be reached
    (or refuses to let a server revoke itself) the local link is deleted anyway and we say so:
    the owner can finish from the website's "Linked servers"."""
    link = await db.get_link(user_id)
    if link is None:
        return {"unlinked": False, "cloud_revoked": False, "message": "This server was not linked."}
    cloud_revoked, message = False, "Unlinked."
    if link.status == "linked" and link.token_enc and link.server_id:
        try:
            await call(user_id, "DELETE", f"/me/servers/{cloud_client.quote_id(link.server_id)}")
            cloud_revoked = True
        except CloudReply as e:
            message = (f"Unlinked here, but the cloud did not confirm revoking it (it answered {e.status}). "
                       "Remove this server under Linked servers on the website.")
        except CloudError as e:
            message = (f"Unlinked here, but the cloud could not be asked to revoke it ({e.code}). "
                       "Remove this server under Linked servers on the website.")
    elif link.status == "linked":
        message = "Unlinked here. Remove this server under Linked servers on the website to revoke it there."
    await db.delete_link(user_id)
    await db.delete_pointers(user_id)
    from . import cloud_forward, cloud_list

    cloud_list.forget(user_id)
    cloud_forward.forget_pages()
    return {"unlinked": True, "cloud_revoked": cloud_revoked, "message": message}


# ── key rotation ─────────────────────────────────────────────────────────────
async def reencrypt_all() -> int:
    """Re-encrypt every stored secret under the primary key. Returns how many rows changed."""
    n = 0
    for user_id, token_enc, device_enc in await db.all_secrets():
        await db.set_secrets(
            user_id,
            cloud_crypto.encrypt(cloud_crypto.decrypt(token_enc)) if token_enc else None,
            cloud_crypto.encrypt(cloud_crypto.decrypt(device_enc)) if device_enc else None,
        )
        n += 1
    return n
