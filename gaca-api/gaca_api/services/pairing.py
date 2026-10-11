"""Pairing: how a phone becomes allowed to use this server.

A GACA server belongs to one person, who has a shell on it. Pairing proves that: they run
`gaca-api pair` on the box, which prints a short-lived one-time code (as text and as a QR code
that also carries the server's address), and the app trades that code for a device credential.
There are no passwords and no accounts to manage. Any number of devices may pair; each gets its
own revocable credential. A device unpairs by revoking it (`gaca-api device revoke`, or the app
switching to another server).

The first pairing on a fresh server also creates the one owner record everything is filed under.

The code is stored hashed, expires, is single-use, and is burned after a few wrong guesses; it is
never sent over the network by the server. No FastAPI, no HTTP — routers/devices.py maps
`SetupRefused` to status codes.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta

from ..db import meta as meta_db
from ..db import users as users_db
from ..models.api_token import ApiTokenDoc
from ..models.base import utcnow
from ..models.user import UserDoc
from . import tokens as token_service
from .setup import CODE_LENGTH, MAX_WRONG_ATTEMPTS, SetupRefused, _ALPHABET, _lock, _normalize, format_code

_KEY = "pair_code"
DEFAULT_TTL_MINUTES = 10
OWNER_EMAIL = "owner@gaca.invalid"  # the owner record needs an address; nothing is ever sent to it


def _digest(code: str) -> str:
    return hashlib.sha256(_normalize(code).encode()).hexdigest()


async def issue_code(ttl_minutes: int = DEFAULT_TTL_MINUTES) -> tuple[str, str]:
    """A fresh pairing code (replacing any earlier one) and when it expires (ISO). The plaintext
    is returned exactly once — only its hash is stored."""
    async with _lock():
        raw = "".join(secrets.choice(_ALPHABET) for _ in range(CODE_LENGTH))
        expires = (utcnow() + timedelta(minutes=ttl_minutes)).isoformat()
        await meta_db.put(_KEY, {"hash": _digest(raw), "expires_at": expires, "wrong": 0})
        return format_code(raw), expires


async def _check_code(code: str) -> None:
    """Raise unless `code` is the live pairing code. Counts and burns wrong guesses."""
    state = await meta_db.get(_KEY)
    if state is None:
        raise SetupRefused("no_code", "No pairing code is active. Run `gaca-api pair` on the server.")
    if utcnow() >= datetime.fromisoformat(state["expires_at"]):
        await meta_db.delete(_KEY)
        raise SetupRefused("expired", "That pairing code has expired. Run `gaca-api pair` for a new one.")
    if not hmac.compare_digest(_digest(code), state["hash"]):
        state["wrong"] += 1
        if state["wrong"] >= MAX_WRONG_ATTEMPTS:
            await meta_db.delete(_KEY)
            raise SetupRefused("burned", "Too many wrong codes. Run `gaca-api pair` for a new one.")
        await meta_db.put(_KEY, state)
        raise SetupRefused("wrong_code", "That pairing code is wrong.")


async def _owner() -> UserDoc:
    owner = await users_db.first()
    if owner is None:
        owner = UserDoc(email=OWNER_EMAIL, display_name="Owner")
        await users_db.insert(owner)
    return owner


async def pair(code: str, device_name: str) -> tuple[ApiTokenDoc, str]:
    """Trade a live pairing code for a device credential: (record, secret shown once)."""
    async with _lock():
        await _check_code(code)
        owner = await _owner()
        doc, secret = await token_service.create_device(owner, device_name.strip()[:64] or "Phone")
        await meta_db.delete(_KEY)
        return doc, secret
