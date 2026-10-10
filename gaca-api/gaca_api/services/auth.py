"""Auth business logic. No FastAPI, no HTTP — just orchestration.

calendarapp and toolshed put these two calls inline in the route, because Beanie
lets a route say `User.find_one(...)` / `user.insert()` directly. gaca-api has a
db/ layer instead, and `routers/sessions.py` already holds the line that routes
do no DB access — so the same two steps live here and the route reads the same.
"""
from __future__ import annotations

import secrets

from ..agent import runtime as agent_runtime
from ..agent.base import AgentBackend
from ..db import users as users_db
from ..models.user import CREW_COLOR_PALETTE, CrewMember, SSHConnection, UserDoc
from ..config import settings
from ..provisioning.select import provisioner_for
from ..utils.auth import hash_password
from .exceptions import CrewNotConfigured, InvalidRequest, NotFound


async def find_by_id(user_id: str) -> UserDoc | None:
    return await users_db.find(user_id)


async def find_by_email(email: str) -> UserDoc | None:
    # Emails are stored lowercase, so look up lowercase: a phone keyboard that capitalises the
    # first letter must not turn a correct sign-in into a 401.
    return await users_db.find_by_email(email.strip().lower())


async def find_by_google_sub(google_sub: str) -> UserDoc | None:
    return await users_db.find_by_google_sub(google_sub)


async def resolve_google_user(*, google_sub: str, email: str) -> UserDoc | None:
    """Sign-in only: the existing owner for this verified Google identity, or None.

    Never creates an account — a server has one owner, made through the setup claim. Links the
    Google identity onto the owner's row when the verified email matches (they claimed with a
    password and are now using the Google button).
    """
    existing = await users_db.find_by_google_sub(google_sub)
    if existing is not None:
        return existing
    by_email = await users_db.find_by_email(email.lower())
    if by_email is None:
        return None
    linked = by_email.model_copy(update={"google_sub": google_sub, "email_verified": True})
    await users_db.replace(linked)
    return linked


async def set_password(user: UserDoc, password: str) -> UserDoc:
    """Replace the owner's password (also gives a Google-only owner one). Used by the
    `gaca-api reset-password` command: shell access to the server is the proof of ownership."""
    updated = user.model_copy(update={"hashed_password": hash_password(password)})
    await users_db.replace(updated)
    return updated


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
        email=email.strip().lower(),
        display_name=name,
        hashed_password=hash_password(password),
    )
    await users_db.insert(user)
    return user


async def set_agent_host(user: UserDoc, host: str) -> UserDoc:
    """Update the scheme+host the connected agent (and every crew member
    under it) lives on — e.g. the box moved to a new address. Does not touch
    `crew`; see `connect_agent` for first-time setup, which does."""
    updated = user.model_copy(update={"agent_host": host})
    await users_db.replace(updated)
    return updated


def find_crew_member(user: UserDoc, name: str | None) -> CrewMember | None:
    """Plain lookup by name — no error if it isn't there (or `name` is None)."""
    if name is None:
        return None
    return next((c for c in user.crew if c.name == name), None)


def _next_color(user: UserDoc) -> str:
    """The first palette color not already in use, cycling once exhausted."""
    used = {c.color for c in user.crew}
    for color in CREW_COLOR_PALETTE:
        if color not in used:
            return color
    return CREW_COLOR_PALETTE[len(user.crew) % len(CREW_COLOR_PALETTE)]


async def connect_agent(
    user: UserDoc, host: str, port: int, api_key: str, name: str = "default"
) -> UserDoc:
    """First-time setup: connect this account to its one Hermes agent.

    Sets `agent_host` and creates `name` as the first crew member (using
    `port`/`api_key`) in one step, so the user gets a working default without
    a separate "now add a crew member" action — the account has something to
    chat with and delegate to the instant this call returns. Adding more
    crew members afterward is `add_crew_member`, unchanged.

    Only valid once: an account with an agent already connected (`agent_host`
    set, or a non-empty `crew`) rejects a second connect — to move to a
    different box, update the host with `set_agent_host`; to run another
    profile on the same box, add a crew member. Re-running "connect" isn't
    either of those.
    """
    if user.agent_host is not None or user.crew:
        raise InvalidRequest(
            "an agent is already connected — update the host to move it, or "
            "add a crew member to run another profile on it"
        )

    member = CrewMember(name=name, port=port, api_key=api_key, color=_next_color(user))
    updated = user.model_copy(
        update={"agent_host": host, "crew": [member], "default_crew": name}
    )
    await users_db.replace(updated)
    return updated


async def add_crew_member(
    user: UserDoc,
    name: str,
    port: int,
    api_key: str,
    color: str | None = None,
    *,
    path: str = "",
) -> UserDoc:
    """Register a new named crew member.

    The first one a user adds becomes their default automatically — there's
    no separate onboarding step for that, and a user with exactly one crew
    member obviously means for it to be used. `color` is auto-assigned from
    the palette (first unused swatch) unless the caller picked one.
    """
    if find_crew_member(user, name) is not None:
        raise InvalidRequest(f"crew member {name!r} already exists")

    member = CrewMember(
        name=name, port=port, api_key=api_key, path=path,
        color=color or _next_color(user),
    )
    updated = user.model_copy(
        update={
            "crew": [*user.crew, member],
            "default_crew": user.default_crew or name,
        }
    )
    await users_db.replace(updated)
    return updated


async def provision_crew_member(
    user: UserDoc,
    name: str,
    color: str | None = None,
    access: SSHConnection | None = None,
) -> UserDoc:
    """Create a brand new Hermes profile on the box and register it as a
    crew member in one step — the caller only ever names it; gaca-api picks
    the port (one past whatever's already running on the box, not just what
    it happens to already know about) and generates the key.

    `access` is optional: pass it the first time (POST /crew's `access`
    field) and it's stored for every call after, so it only has to be given
    once. Raises CrewNotConfigured if neither this call nor an earlier one
    ever supplied it. Checked against gaca-api's own crew list before ever
    touching the box — a duplicate name is a 422 regardless of whether the
    box is reachable; Hermes' own `profile create` is the authority on
    whether the name collides there too, surfaced as SSHError (mapped to
    502) if so.
    """
    if access is not None and settings.agent_access == "ssh":
        user = await set_ssh_connection(
            user, access.host, access.port, access.username, access.private_key
        )
    provisioner = provisioner_for(user)
    if provisioner is None:
        raise CrewNotConfigured(
            "gaca-api has no access to create a crew member on this box yet "
            "— include `access` on this call"
        )
    if find_crew_member(user, name) is not None:
        raise InvalidRequest(f"crew member {name!r} already exists")

    # `candidate_port` is only used if this Hermes runs a gateway per profile; if it serves
    # every profile from one gateway, create_profile reports that address instead.
    candidate_port = await provisioner.next_free_port()
    api_key = secrets.token_hex(32)
    port, path = await provisioner.create_profile(name, candidate_port, api_key)
    return await add_crew_member(user, name, port, api_key, color, path=path)


async def update_crew_member(
    user: UserDoc,
    name: str,
    port: int,
    api_key: str,
    color: str | None = None,
    *,
    path: str | None = None,
) -> UserDoc:
    """Replace an existing crew member's port and key (and color, if given).

    Port and key are required together, same no-partial-update rule the
    single-agent config used to have; color alone can't be changed this way
    without also resupplying both — keeps one call shape for the whole row.
    """
    existing = find_crew_member(user, name)
    if existing is None:
        raise NotFound("crew member", name)

    crew = [
        CrewMember(
            name=name, port=port, api_key=api_key, color=color or c.color,
            # Not mentioned = unchanged; this must never silently reset a member's address.
            path=c.path if path is None else path,
        )
        if c.name == name
        else c
        for c in user.crew
    ]
    updated = user.model_copy(update={"crew": crew})
    await users_db.replace(updated)
    return updated


async def remove_crew_member(user: UserDoc, name: str) -> UserDoc:
    """Delete a named crew member.

    Deleting the current default while others remain is rejected rather than
    silently picking a new default — that's a real decision the user should
    make explicitly via set_default_crew_member. Deleting the last one clears
    the default too, since there's nothing left for it to name.
    """
    if find_crew_member(user, name) is None:
        raise NotFound("crew member", name)

    remaining = [c for c in user.crew if c.name != name]
    if name == user.default_crew and remaining:
        raise InvalidRequest(
            f"{name!r} is the default crew member — set a different default "
            "before removing it"
        )

    updated = user.model_copy(
        update={
            "crew": remaining,
            "default_crew": user.default_crew if remaining else None,
        }
    )
    await users_db.replace(updated)
    return updated


async def set_default_crew_member(user: UserDoc, name: str) -> UserDoc:
    if find_crew_member(user, name) is None:
        raise NotFound("crew member", name)
    updated = user.model_copy(update={"default_crew": name})
    await users_db.replace(updated)
    return updated


def resolve_crew_member(user: UserDoc, requested: str | None) -> CrewMember:
    """Which crew member handles a plain session: `requested` if given (a
    real error if it doesn't exist), else the account's `default_crew`.

    Raises CrewNotConfigured if neither resolves to a real crew member.
    """
    if requested is not None:
        member = find_crew_member(user, requested)
        if member is None:
            raise InvalidRequest(f"crew member {requested!r} is not configured")
        return member

    member = find_crew_member(user, user.default_crew)
    if member is not None:
        return member

    raise CrewNotConfigured(
        "no crew member to use — connect your agent first via "
        "POST /api/agent/connect, or name one explicitly"
    )


def resolve_crew_for_delegate(
    user: UserDoc, requested: str | None, alert_crew: str | None
) -> CrewMember:
    """Which crew member handles a delegation, in priority order:

    1. `requested` — an explicit, deliberate choice from the caller. Naming
       one that doesn't exist is a real error, not something to paper over.
    2. `alert_crew` — the alert's own `crew` field, soft-matched: it's
       advisory metadata (same trust level as `source`), so a stale or
       unmatched value just falls through to the next step rather than
       erroring.
    3. The account's `default_crew`.

    Raises CrewNotConfigured if none of the above resolves to a real member.
    """
    if requested is not None:
        return resolve_crew_member(user, requested)

    if alert_crew is not None:
        member = find_crew_member(user, alert_crew)
        if member is not None:
            return member

    return resolve_crew_member(user, None)


def backend_for_crew(user: UserDoc, crew_member: CrewMember) -> AgentBackend:
    """The AgentBackend for one of this user's crew members."""
    if not user.agent_host:
        raise CrewNotConfigured(
            "no agent connected yet — connect one first via "
            "POST /api/agent/connect"
        )
    base_url = f"{user.agent_host}:{crew_member.port}{crew_member.path}"
    return agent_runtime.backend_for(base_url, crew_member.api_key)


async def list_skills(user: UserDoc, name: str):
    """The named crew member's skills, straight from its own Hermes gateway."""
    member = find_crew_member(user, name)
    if member is None:
        raise NotFound("crew member", name)
    backend = backend_for_crew(user, member)
    return await backend.list_skills()


# ── Reaching the box directly (creation, discovery) ──────────────────
# Internally SSH (gaca_api/provisioning/ssh.py) — deliberately not named
# that here or in anything a router returns. See CrewAccess's docstring.

async def set_ssh_connection(
    user: UserDoc, host: str, port: int, username: str, private_key: str
) -> UserDoc:
    """Store how gaca-api should reach this user's Hermes box directly.

    Not validated here — a bad credential is only discoverable by actually
    using it, which the first real `provision_crew_member`/`discover_crew`
    call does. Called by both, when their caller supplies `access`, rather
    than exposed as its own endpoint — see CrewAccess's docstring for why.
    """
    updated = user.model_copy(
        update={
            "ssh": SSHConnection(
                host=host, port=port, username=username, private_key=private_key
            )
        }
    )
    await users_db.replace(updated)
    return updated


async def discover_crew(
    user: UserDoc, access: SSHConnection | None = None
) -> UserDoc:
    """Find crew members that already exist on the box — set up by hand, or
    by any means other than this account's own POST /crew — and register
    them, so pre-existing infrastructure isn't invisible to gaca-api just
    because gaca-api didn't create it.

    `access` is optional: pass it the first time (POST /crew/discover's
    `access` field) and it's stored for every call after. Raises
    CrewNotConfigured if neither this call nor an earlier one ever supplied
    it.

    Read-only against the box (see ssh.discover_profiles); the only writes
    are to this account's own row. Never overwrites a crew member gaca-api
    already knows by that name — a name collision keeps its key and color —
    with one exception: if the box reports the same key at a new address, the
    member has moved and its address is updated. Sets `agent_host` from the SSH host if unset (a
    discovery with nothing connected yet doubles as first-time connect),
    and `default_crew` to "default" if that name was found and nothing was
    already the default.
    """
    if access is not None and settings.agent_access == "ssh":
        user = await set_ssh_connection(
            user, access.host, access.port, access.username, access.private_key
        )
    provisioner = provisioner_for(user)
    if provisioner is None:
        raise CrewNotConfigured(
            "gaca-api has no access to discover crew members on this box "
            "yet — include `access` on this call"
        )

    found = await provisioner.discover_profiles()

    updated = user
    for name, info in found.items():
        known = find_crew_member(updated, name)
        if known is not None:
            # Already known: never overwrite its key or color. But if the box reports the SAME
            # key at a different address, it's the same crew member that Hermes has moved (e.g.
            # from its own port to /p/<name> on the shared gateway) — follow it, or it would be
            # unreachable until someone re-typed the address by hand.
            address = (info["port"], info.get("path", ""))
            if known.api_key == info["api_key"] and (known.port, known.path) != address:
                moved = known.model_copy(update={"port": address[0], "path": address[1]})
                updated = updated.model_copy(
                    update={"crew": [moved if c.name == name else c for c in updated.crew]}
                )
            continue
        # _next_color reads `updated.crew`, so two newly-discovered profiles
        # in the same call still get distinct colors, not both the same one.
        member = CrewMember(
            name=name, port=info["port"], api_key=info["api_key"],
            path=info.get("path", ""), color=_next_color(updated),
        )
        updated = updated.model_copy(update={"crew": [*updated.crew, member]})

    update_fields: dict = {}
    if updated.agent_host is None:
        update_fields["agent_host"] = provisioner.default_agent_host
    if updated.default_crew is None and find_crew_member(updated, "default") is not None:
        update_fields["default_crew"] = "default"
    if update_fields:
        updated = updated.model_copy(update=update_fields)

    if updated is not user:
        await users_db.replace(updated)
    return updated
