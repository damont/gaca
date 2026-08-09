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


async def find_by_email(email: str) -> UserDoc | None:
    return await users_db.find_by_email(email)


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
