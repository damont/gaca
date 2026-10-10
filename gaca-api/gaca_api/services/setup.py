"""First-run setup: one owner per server, claimed once with a one-time setup code.

A GACA server belongs to exactly one person (they host their own). Until that person
claims it, whoever reaches the URL first could — so claiming needs a code only the
operator can see: it is printed on the server's console (`gaca-api setup-code`) and never
sent over the network. The code is stored hashed, expires, is single-use, and is burned
after a few wrong guesses. Once an owner exists the claim door is shut for good; every
later device just signs in.

No FastAPI, no HTTP — routers/setup.py maps `SetupRefused` to status codes.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import weakref
from datetime import timedelta

from ..db import meta as meta_db
from ..db import users as users_db
from ..models.base import utcnow
from ..models.user import UserDoc
from . import auth as auth_service

_KEY = "setup_code"
# No 0/O/1/I/L: the code is read off a screen and typed on a phone when a QR isn't handy.
_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 16
DEFAULT_TTL_MINUTES = 60
MAX_WRONG_ATTEMPTS = 5


class SetupRefused(Exception):
    """A setup/claim request that must not proceed. `reason` is stable for callers/tests."""

    def __init__(self, reason: str, detail: str):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = weakref.WeakKeyDictionary()


def _lock() -> asyncio.Lock:
    """One lock per event loop: two simultaneous claims must not both succeed."""
    loop = asyncio.get_running_loop()
    if loop not in _locks:
        _locks[loop] = asyncio.Lock()
    return _locks[loop]


def _normalize(code: str) -> str:
    return "".join(ch for ch in code.upper() if ch.isalnum())


def _digest(code: str) -> str:
    return hashlib.sha256(_normalize(code).encode()).hexdigest()


def format_code(raw: str) -> str:
    return "-".join(raw[i : i + 4] for i in range(0, len(raw), 4))


async def is_claimed() -> bool:
    return await users_db.count() > 0


async def issue_code(ttl_minutes: int = DEFAULT_TTL_MINUTES) -> tuple[str, str]:
    """A fresh setup code (replacing any earlier one) and when it expires (ISO). The plaintext
    is returned exactly once — only its hash is stored. Refused once an owner exists."""
    async with _lock():
        if await is_claimed():
            raise SetupRefused("claimed", "This server already has an owner.")
        raw = "".join(secrets.choice(_ALPHABET) for _ in range(CODE_LENGTH))
        expires = (utcnow() + timedelta(minutes=ttl_minutes)).isoformat()
        await meta_db.put(_KEY, {"hash": _digest(raw), "expires_at": expires, "wrong": 0})
        return format_code(raw), expires


async def _check_code(code: str) -> None:
    """Raise unless `code` is the live setup code. Counts and burns wrong guesses."""
    state = await meta_db.get(_KEY)
    if state is None:
        raise SetupRefused(
            "no_code", "No setup code is active. Run `gaca-api setup-code` on the server."
        )
    if utcnow().isoformat() > state["expires_at"]:
        await meta_db.delete(_KEY)
        raise SetupRefused("expired", "That setup code has expired. Run `gaca-api setup-code` for a new one.")
    if not hmac.compare_digest(_digest(code), state["hash"]):
        state["wrong"] += 1
        if state["wrong"] >= MAX_WRONG_ATTEMPTS:
            await meta_db.delete(_KEY)
            raise SetupRefused(
                "burned", "Too many wrong codes. Run `gaca-api setup-code` for a new one."
            )
        await meta_db.put(_KEY, state)
        raise SetupRefused("wrong_code", "That setup code is wrong.")


async def claim_with_password(code: str, name: str, email: str, password: str) -> UserDoc:
    async with _lock():
        if await is_claimed():
            raise SetupRefused("claimed", "This server already has an owner.")
        await _check_code(code)
        user = await auth_service.create_user(name=name, email=email.lower(), password=password)
        await meta_db.delete(_KEY)
        return user


async def claim_with_google(code: str, *, google_sub: str, email: str, name: str) -> UserDoc:
    async with _lock():
        if await is_claimed():
            raise SetupRefused("claimed", "This server already has an owner.")
        await _check_code(code)
        user = await auth_service.upsert_google_user(google_sub=google_sub, email=email, name=name)
        await meta_db.delete(_KEY)
        return user
