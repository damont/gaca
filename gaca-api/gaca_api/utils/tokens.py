"""API tokens: format, hashing, and what each scope may call.

An API token is for software acting for the owner (Hermes skills posting alerts,
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
    "briefings": [("*", r"/api/briefings(/.*)?")],
    "artifacts": [("*", r"/api/artifacts(/.*)?")],
    # OPT-IN, never in DEFAULT_SCOPES. With it a token (Hermes) can see the link status, browse the
    # template gallery, and sees/uses cloud-homed gacabits through /api/artifacts (their pointers are
    # invisible to a token without it). Even with it a token can NEVER link, unlink, redeem an invite
    # or install a template: those are the paired app's alone (the "Trust and add" tap is the consent).
    # The same goes for everything in Part 8b: move to the cloud, bring home, members, invite links, publish.
    "cloud": [
        ("GET", r"/api/cloud/link"),
        ("GET", r"/api/cloud/templates"),
        ("GET", r"/api/cloud/templates/[^/]+"),
    ],
    # Read-only on purpose: the heartbeat may learn its limits but never change them.
    "heartbeat": [("GET", r"/api/heartbeat/settings")],
}
SCOPES = tuple(SCOPE_RULES)
# What a token gets when nobody says otherwise (`gaca-api token create`, `hermes connect`): every
# scope except the opt-in ones. Grant `cloud` deliberately: `gaca-api token create --name X --scopes ...,cloud`.
OPT_IN_SCOPES = ("cloud",)
DEFAULT_SCOPES = tuple(s for s in SCOPES if s not in OPT_IN_SCOPES)
# Allowed with any scope: lets a client check "is my token good, and whose is it".
# `GET /api/cloud/link` is here so an agent can always ask "are we connected to the cloud, and may I use it?"
# (the answer is trimmed for a token without the `cloud` scope, see routers/cloud.py).
ALWAYS = [("GET", r"/api/auth/me"), ("GET", r"/api/cloud/link")]


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
