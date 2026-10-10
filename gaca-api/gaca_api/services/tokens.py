"""API token lifecycle: mint, list, revoke, and resolve a presented token to its owner.

No FastAPI, no HTTP. The plaintext token exists only in the return value of `create` — what is
stored is its hash — so a database leak does not leak working tokens.
"""
from __future__ import annotations

from datetime import timedelta

from ..db import api_tokens as tokens_db
from ..db import users as users_db
from ..models.api_token import ApiTokenDoc
from ..models.base import utcnow
from ..models.user import UserDoc
from ..utils import tokens as token_utils
from .exceptions import InvalidRequest, NotFound

# last_used_at is a "is this token still alive" hint, not an audit log: update it at most this often.
_TOUCH_EVERY = timedelta(minutes=1)


async def create(
    user: UserDoc, name: str, scopes: list[str], expires_in_days: int | None = None
) -> tuple[ApiTokenDoc, str]:
    unknown = [s for s in scopes if s not in token_utils.SCOPES]
    if not scopes or unknown:
        raise InvalidRequest(f"scopes must be a non-empty subset of {list(token_utils.SCOPES)}")
    plaintext = token_utils.generate()
    doc = ApiTokenDoc(
        user_id=user.id, name=name, scopes=sorted(set(scopes)),
        expires_at=(utcnow() + timedelta(days=expires_in_days)) if expires_in_days else None,
    )
    await tokens_db.insert(doc, token_utils.digest(plaintext))
    return doc, plaintext


async def list_tokens(user: UserDoc) -> list[ApiTokenDoc]:
    return await tokens_db.list_for_user(user.id)


async def revoke(user: UserDoc, token_id: str) -> None:
    if not await tokens_db.revoke(user.id, token_id, utcnow()):
        raise NotFound("token", token_id)


async def revoke_older_with_name(user: UserDoc, name: str, *, keep_id: str) -> int:
    """Revoke every live token called `name` except `keep_id` — so re-running a setup command
    rotates a credential instead of leaving a trail of valid ones behind."""
    return await tokens_db.revoke_active_by_name(user.id, name, utcnow(), keep_id=keep_id)


async def authenticate(plaintext: str) -> tuple[UserDoc, ApiTokenDoc] | None:
    """The owner and token record for a presented API token, or None if it is unknown,
    revoked, expired, or its user is gone/disabled."""
    doc = await tokens_db.find_by_hash(token_utils.digest(plaintext))
    if doc is None or doc.revoked_at is not None:
        return None
    now = utcnow()
    if doc.expires_at is not None and now >= doc.expires_at:
        return None
    user = await users_db.find(doc.user_id)
    if user is None or not user.is_active:
        return None
    if doc.last_used_at is None or now - doc.last_used_at >= _TOUCH_EVERY:
        await tokens_db.touch(doc.id, now)
    return user, doc
