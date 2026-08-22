"""Auth business logic. No FastAPI, no HTTP — just orchestration.

calendarapp and toolshed put these two calls inline in the route, because Beanie
lets a route say `User.find_one(...)` / `user.insert()` directly. gaca-api has a
db/ layer instead, and `routers/sessions.py` already holds the line that routes
do no DB access — so the same two steps live here and the route reads the same.
"""
from __future__ import annotations

from ..db import users as users_db
from ..models.user import UserDoc
from ..utils.auth import hash_password


async def find_by_id(user_id: str) -> UserDoc | None:
    return await users_db.find(user_id)


async def find_by_email(email: str) -> UserDoc | None:
    return await users_db.find_by_email(email)


async def find_by_google_sub(google_sub: str) -> UserDoc | None:
    return await users_db.find_by_google_sub(google_sub)


async def upsert_google_user(
    *, google_sub: str, email: str, name: str
) -> UserDoc:
    """Resolve a verified Google identity to a user, creating or linking as needed.

    Three cases, in priority order:

    1. We already know this `sub` — return that user.
    2. We know the email but not the sub: the person signed up with a password
       first and is now using the Google button. Link the two onto one row
       rather than making a second account they'd have to keep straight.
    3. Neither — create an account with no password at all. They can only ever
       sign in through Google, which is why `hashed_password` is optional.
    """
    existing = await users_db.find_by_google_sub(google_sub)
    if existing is not None:
        return existing

    by_email = await users_db.find_by_email(email)
    if by_email is not None:
        linked = by_email.model_copy(
            update={"google_sub": google_sub, "email_verified": True}
        )
        await users_db.replace(linked)
        return linked

    user = UserDoc(
        email=email,
        display_name=name,
        hashed_password=None,
        google_sub=google_sub,
        email_verified=True,
    )
    await users_db.insert(user)
    return user


async def create_user(name: str, email: str, password: str) -> UserDoc:
    """Hash the password and persist the new user.

    The plaintext stops here — it is hashed on the way in and never stored.
    """
    user = UserDoc(
        email=email,
        display_name=name,
        hashed_password=hash_password(password),
    )
    await users_db.insert(user)
    return user


async def set_agent_config(user: UserDoc, base_url: str, api_key: str) -> UserDoc:
    """Point this user's session/delegate calls at their own agent.

    Both are required together — a base URL with no key or vice versa isn't a
    usable configuration, so there's no partial-update path here.
    """
    updated = user.model_copy(
        update={"agent_base_url": base_url, "agent_api_key": api_key}
    )
    await users_db.replace(updated)
    return updated
