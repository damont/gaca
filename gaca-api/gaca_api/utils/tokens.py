"""API tokens: format, hashing, and what each scope may call.

An API token is for software acting for the owner (Hermes skills posting alerts, lists,
briefings, artifacts) — not for a person, and not for the app. It is deliberately narrower than
a paired device (the app), which has the owner's full access:

  * it can only call the endpoints its scopes name, below; everything else (chat, crew, the agent
    connection, push, account, and token management itself) answers 403;
  * on alerts it can create and read, but not act/dismiss/snooze/delegate/take-to-chat — those are
    the owner's decisions, made in the app;
  * it can be revoked individually, and it never expires silently (no expiry unless asked for).
"""
from __future__ import annotations

import hashlib
import re
import secrets

PREFIX = "gaca_pat_"
# A paired device (the app) holds this kind instead: the owner's own full access, individually
# revocable. Different prefix and a reserved scope so the two kinds can never be mistaken for each other.
DEVICE_PREFIX = "gaca_dev_"
DEVICE_SCOPE = "device"

# scope -> [(method or "*", fullmatch regex on the request path)]
SCOPE_RULES: dict[str, list[tuple[str, str]]] = {
    "alerts": [
        ("POST", r"/api/alerts"),
        ("GET", r"/api/alerts"),
        ("GET", r"/api/alerts/[^/]+"),
    ],
    "lists": [("*", r"/api/lists(/.*)?")],
    "briefings": [("*", r"/api/briefings(/.*)?")],
    "artifacts": [("*", r"/api/artifacts(/.*)?")],
}
SCOPES = tuple(SCOPE_RULES)
# Allowed with any scope: lets a client check "is my token good, and whose is it".
ALWAYS = [("GET", r"/api/auth/me")]


def generate() -> str:
    return PREFIX + secrets.token_urlsafe(32)


def generate_device() -> str:
    return DEVICE_PREFIX + secrets.token_urlsafe(32)


def looks_like_token(value: str) -> bool:
    return value.startswith(PREFIX)


def looks_like_device(value: str) -> bool:
    return value.startswith(DEVICE_PREFIX)


def digest(token: str) -> str:
    # Tokens are 256 random bits, so a fast hash is enough (no password-style stretching needed).
    return hashlib.sha256(token.encode()).hexdigest()


def allowed(scopes: list[str], method: str, path: str) -> bool:
    # Fail closed on anything that is not a plain path: dot segments and empty segments are
    # how a prefix check gets walked out of its scope.
    segments = path.split("/")[1:]
    if ".." in segments or "." in segments or "" in segments[:-1]:
        return False
    rules = list(ALWAYS)
    for scope in scopes:
        rules.extend(SCOPE_RULES.get(scope, []))
    return any(m in ("*", method.upper()) and re.fullmatch(rx, path) for m, rx in rules)
