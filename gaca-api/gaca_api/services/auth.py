"""Auth business logic. No FastAPI, no HTTP — just orchestration.

calendarapp and toolshed put these two calls inline in the route, because Beanie
lets a route say `User.find_one(...)` / `user.insert()` directly. gaca-api has a
db/ layer instead, and `routers/sessions.py` already holds the line that routes
do no DB access — so the same two steps live here and the route reads the same.
"""
from __future__ import annotations

from ..agent import runtime as agent_runtime
from ..agent.base import AgentBackend
from ..db import users as users_db
from ..models.user import AgentProfile, UserDoc
from ..utils.auth import hash_password
from .exceptions import AgentNotConfigured, InvalidRequest, NotFound


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


async def set_agent_host(user: UserDoc, host: str) -> UserDoc:
    """Set the scheme+host shared by every one of this user's agents."""
    updated = user.model_copy(update={"agent_host": host})
    await users_db.replace(updated)
    return updated


def find_agent(user: UserDoc, name: str) -> AgentProfile | None:
    """Plain lookup by name — no error if it isn't there."""
    return next((a for a in user.agents if a.name == name), None)


async def add_agent(user: UserDoc, name: str, port: int, api_key: str) -> UserDoc:
    """Register a new named agent.

    The first agent a user adds becomes their default automatically — there's
    no separate onboarding step for that, and a user with exactly one agent
    obviously means for it to be used.
    """
    if find_agent(user, name) is not None:
        raise InvalidRequest(f"agent {name!r} already exists")

    profile = AgentProfile(name=name, port=port, api_key=api_key)
    updated = user.model_copy(
        update={
            "agents": [*user.agents, profile],
            "default_agent": user.default_agent or name,
        }
    )
    await users_db.replace(updated)
    return updated


async def update_agent(user: UserDoc, name: str, port: int, api_key: str) -> UserDoc:
    """Replace an existing agent's port and key. Both required together, same
    no-partial-update rule the single-agent config used to have."""
    if find_agent(user, name) is None:
        raise NotFound("agent", name)

    agents = [
        AgentProfile(name=name, port=port, api_key=api_key) if a.name == name else a
        for a in user.agents
    ]
    updated = user.model_copy(update={"agents": agents})
    await users_db.replace(updated)
    return updated


async def remove_agent(user: UserDoc, name: str) -> UserDoc:
    """Delete a named agent.

    Deleting the current default while others remain is rejected rather than
    silently picking a new default — that's a real decision the user should
    make explicitly via set_default_agent. Deleting the last agent clears the
    default too, since there's nothing left for it to name.
    """
    if find_agent(user, name) is None:
        raise NotFound("agent", name)

    remaining = [a for a in user.agents if a.name != name]
    if name == user.default_agent and remaining:
        raise InvalidRequest(
            f"{name!r} is the default agent — set a different default before "
            "deleting it"
        )

    updated = user.model_copy(
        update={
            "agents": remaining,
            "default_agent": user.default_agent if remaining else None,
        }
    )
    await users_db.replace(updated)
    return updated


async def set_default_agent(user: UserDoc, name: str) -> UserDoc:
    if find_agent(user, name) is None:
        raise NotFound("agent", name)
    updated = user.model_copy(update={"default_agent": name})
    await users_db.replace(updated)
    return updated


def resolve_agent_for_delegate(
    user: UserDoc, requested: str | None, alert_agent: str | None
) -> AgentProfile:
    """Which agent handles a delegation, in priority order:

    1. `requested` — an explicit, deliberate choice from the caller. Naming
       one that doesn't exist is a real error, not something to paper over.
    2. `alert_agent` — the alert's own `agent` field, soft-matched: it's
       advisory metadata (same trust level as `source`), so a stale or
       unmatched value just falls through to the next step rather than
       erroring.
    3. The account's `default_agent`.

    Raises AgentNotConfigured if none of the above resolves to a real agent.
    """
    if requested is not None:
        profile = find_agent(user, requested)
        if profile is None:
            raise InvalidRequest(f"agent {requested!r} is not configured")
        return profile

    if alert_agent is not None:
        profile = find_agent(user, alert_agent)
        if profile is not None:
            return profile

    if user.default_agent is not None:
        profile = find_agent(user, user.default_agent)
        if profile is not None:
            return profile

    raise AgentNotConfigured()


def backend_for_agent(user: UserDoc, agent: AgentProfile) -> AgentBackend:
    """The AgentBackend for one of this user's agents."""
    if not user.agent_host:
        raise AgentNotConfigured()
    base_url = f"{user.agent_host}:{agent.port}"
    return agent_runtime.backend_for(base_url, agent.api_key)
