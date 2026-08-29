"""Database model for a user — the stored shape (system of record).

Note what is NOT here: the plaintext password. It never reaches this model, is
never written to SQLite, and is never logged. Registration hashes it on the way
in and the hash is all we keep.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from .base import new_id, utcnow


class AgentProfile(BaseModel):
    """One named Hermes profile this user can direct sessions/delegations at.

    Each profile runs on its own port on the user's shared `agent_host`, with
    its own `api_key` — separate ports because the user runs several Hermes
    profiles (e.g. "toolshed", "trading") on one box, each addressed
    independently rather than through Hermes' path-prefix multiplexing.
    """

    name: str
    port: int
    api_key: str


class UserDoc(BaseModel):
    id: str = Field(default_factory=new_id)
    # Stored lowercase — the service normalizes before both writes and lookups,
    # so "David@x.com" and "david@x.com" are the same account. The UNIQUE index
    # on this column is case-sensitive, so without normalizing you'd get two.
    email: str
    display_name: str
    # Argon2 hash. Carries its own salt and parameters inside the string, so
    # there is no separate salt column to keep in sync.
    #
    # Optional because a Google-only account never sets a password. The login
    # route checks this before verifying, so such an account can't be signed
    # into with an empty password.
    hashed_password: str | None = None
    # Google's stable per-user id (the ID token's `sub`). Stable across email
    # changes, which is why we key on it rather than on the address. Backed by a
    # partial unique index so accounts without one don't all collide on NULL.
    google_sub: str | None = None
    # Defaults True so every account that predates Google sign-in is treated as
    # verified rather than locked out. Nothing sets it False yet — mandatory
    # verification needs SMTP, which this app doesn't have. Google accounts are
    # genuinely verified, via the ID token's own email_verified claim.
    email_verified: bool = True
    is_active: bool = True
    created_at: datetime = Field(default_factory=utcnow)

    # Each operator brings their own Hermes (or future engine) instance(s) —
    # there is no shared, app-wide agent target. `agent_host` is the scheme
    # and host shared by every agent below (e.g. "http://david-claw", no
    # port); each AgentProfile supplies its own port and key. Unset/empty
    # until the user configures at least one agent via POST
    # /api/auth/agents; every delegate/chat call until then raises
    # AgentNotConfigured. Stored plaintext in `doc` like the rest of this
    # model's non-password fields — no weaker than the single shared
    # GACA_AGENT_API_KEY this replaces, which also lived in plaintext in an
    # env file.
    agent_host: str | None = None
    agents: list[AgentProfile] = Field(default_factory=list)
    # Which agent a delegate/session uses when nothing more specific applies
    # (no explicit override, and the alert's own `agent` doesn't match a
    # configured name). Cleared automatically if the agent it names is
    # removed.
    default_agent: str | None = None
