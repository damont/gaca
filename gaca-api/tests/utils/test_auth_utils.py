"""Tests for password hashing, token minting/validation, and get_current_user.

The token tests are the security-relevant ones: a token that is expired, signed
with the wrong secret, signed with an unexpected algorithm, or edited in transit
must all be rejected, and rejected identically — the caller never learns which.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi import HTTPException

from gaca_api.config import settings
from gaca_api.db import client
from gaca_api.db import users as users_db
from gaca_api.models.user import UserDoc
from gaca_api.utils.auth import (
    create_access_token,
    decode_token,
    get_current_user,
    hash_password,
    verify_password,
)

# PyJWT emits InsecureKeyLengthWarning below 32 bytes for HS256 and 64 for
# HS512 (RFC 7518 §3.2). Keep the test keys past both so the suite stays quiet
# and exercises a realistically-sized secret.
SECRET = "test-secret-long-enough-for-hs256-and-hs512-so-pyjwt-stays-quiet-01"
OTHER_SECRET = "another-secret-long-enough-for-hs256-and-hs512-and-clearly-not-ours"


# ── password hashing ────────────────────────────────────────────────

def test_hash_is_salted_and_verifies():
    a = hash_password("hunter2")
    b = hash_password("hunter2")
    # A fresh random salt per call, embedded in the hash string — so the same
    # password never produces the same stored value twice.
    assert a != b
    assert verify_password("hunter2", a)
    assert verify_password("hunter2", b)


def test_verify_rejects_wrong_password():
    hashed = hash_password("hunter2")
    assert not verify_password("hunter3", hashed)
    assert not verify_password("", hashed)


def test_hash_does_not_contain_the_password():
    assert "hunter2" not in hash_password("hunter2")


# ── tokens ──────────────────────────────────────────────────────────

def test_token_round_trip(monkeypatch):
    monkeypatch.setattr(settings, "jwt_secret", SECRET)
    assert decode_token(create_access_token("user-123")) == "user-123"


def test_expired_token_rejected(monkeypatch):
    monkeypatch.setattr(settings, "jwt_secret", SECRET)
    token = create_access_token("user-123", expires_delta=timedelta(seconds=-1))
    assert decode_token(token) is None


def test_token_signed_with_another_secret_rejected(monkeypatch):
    monkeypatch.setattr(settings, "jwt_secret", SECRET)
    token = create_access_token("user-123")
    # Someone else's signing key must not open our door.
    monkeypatch.setattr(settings, "jwt_secret", OTHER_SECRET)
    assert decode_token(token) is None


def test_token_with_unexpected_algorithm_rejected(monkeypatch):
    """The `algorithms=` pin in decode_token is what makes this fail.

    Without it the library would take the algorithm from the token's own header,
    which is how the classic `"alg": "none"` forgery works.
    """
    monkeypatch.setattr(settings, "jwt_secret", SECRET)
    monkeypatch.setattr(settings, "jwt_algorithm", "HS256")
    forged = jwt.encode({"sub": "user-123"}, SECRET, algorithm="HS512")
    assert decode_token(forged) is None


def test_tampered_token_rejected(monkeypatch):
    monkeypatch.setattr(settings, "jwt_secret", SECRET)
    token = create_access_token("user-123")
    header, payload, signature = token.split(".")
    # Re-sign nothing, just corrupt one character of the signature.
    tampered = f"{header}.{payload}.{signature[:-1]}{'A' if signature[-1] != 'A' else 'B'}"
    assert decode_token(tampered) is None


def test_long_lived_agent_token_decodes_like_a_session_token(monkeypatch):
    """An agent token is not a different kind of credential.

    Same format, same signature, same verification path — only the expiry
    differs. That is what lets get_current_user accept it with no special case.
    """
    monkeypatch.setattr(settings, "jwt_secret", SECRET)
    token = create_access_token("user-123", expires_delta=timedelta(days=365))
    assert decode_token(token) == "user-123"


def test_expires_delta_overrides_the_session_default(monkeypatch):
    monkeypatch.setattr(settings, "jwt_secret", SECRET)
    monkeypatch.setattr(settings, "jwt_expire_minutes", 1)  # session default: 1 min
    token = create_access_token("user-123", expires_delta=timedelta(days=90))
    exp = jwt.decode(token, SECRET, algorithms=[settings.jwt_algorithm])["exp"]
    assert exp - datetime.now(timezone.utc).timestamp() > 89 * 86400


def test_garbage_token_rejected(monkeypatch):
    monkeypatch.setattr(settings, "jwt_secret", SECRET)
    for junk in ("", "not-a-token", "a.b.c"):
        assert decode_token(junk) is None


# ── get_current_user ────────────────────────────────────────────────
#
# Called directly with a token string: `Depends(oauth2_scheme)` is only resolved
# by FastAPI when serving a request, so the default is bypassed here.

def _seed(tmp_path, monkeypatch, *, is_active: bool = True) -> UserDoc:
    """Create a temp DB holding one user; return it."""
    settings.db_path = str(tmp_path / "t.db")
    monkeypatch.setattr(settings, "jwt_secret", SECRET)
    user = UserDoc(
        email="david@example.com",
        display_name="David",
        hashed_password=hash_password("hunter2"),
        is_active=is_active,
    )

    async def main():
        await client.connect()
        try:
            await users_db.insert(user)
        finally:
            await client.close()

    asyncio.run(main())
    return user


def _resolve(token: str):
    async def main():
        await client.connect()
        try:
            return await get_current_user(token)
        finally:
            await client.close()

    return asyncio.run(main())


def test_get_current_user_returns_the_named_user(tmp_path, monkeypatch):
    user = _seed(tmp_path, monkeypatch)
    resolved = _resolve(create_access_token(user.id))
    assert resolved.id == user.id
    assert resolved.email == "david@example.com"


def test_get_current_user_rejects_unknown_user(tmp_path, monkeypatch):
    """A perfectly valid token naming a user we've never heard of."""
    _seed(tmp_path, monkeypatch)
    with pytest.raises(HTTPException) as exc:
        _resolve(create_access_token("no-such-user"))
    assert exc.value.status_code == 401


def test_get_current_user_rejects_inactive_user(tmp_path, monkeypatch):
    user = _seed(tmp_path, monkeypatch, is_active=False)
    with pytest.raises(HTTPException) as exc:
        _resolve(create_access_token(user.id))
    assert exc.value.status_code == 401


def test_get_current_user_rejects_bad_token(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    with pytest.raises(HTTPException) as exc:
        _resolve("not-a-token")
    assert exc.value.status_code == 401
