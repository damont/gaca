"""Database model for a user — the stored shape (system of record).

Note what is NOT here: the plaintext password. It never reaches this model, is
never written to SQLite, and is never logged. Registration hashes it on the way
in and the hash is all we keep.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, model_validator

from .base import new_id, utcnow

# Muted, low-saturation hues (matching the app's existing slate/severity
# palette) auto-assigned to a crew member on creation so identity is visually
# consistent everywhere it shows up (avatar, chat accent, bubbles, settings
# row) without the user having to pick a color by hand. Cycles once a user has
# more crew members than swatches.
CREW_COLOR_PALETTE: tuple[str, ...] = (
    "#4E7093",  # harbor
    "#6B8F5A",  # moss
    "#B97A56",  # clay
    "#8A6A9E",  # plum
    "#3F8E93",  # teal
    "#B0923F",  # ochre
)


class CrewMember(BaseModel):
    """One named Hermes profile under the user's connected agent.

    The user connects to one Hermes agent (POST /api/agent/connect), which
    creates the first ("default") crew member automatically; more can be
    added afterward. Each one runs on its own port on the account's shared
    `agent_host`, with its own `api_key` — separate ports because a Hermes
    profile is, underneath, a separate Hermes process, addressed
    independently rather than through Hermes' own path-prefix multiplexing.
    That's an implementation detail, not something the API surfaces: from
    the outside, this is just "another crew member."
    """

    name: str
    port: int
    api_key: str
    # Defaults to the first palette color so an old row migrated without one
    # (see UserDoc._migrate_legacy_agent_fields) still validates; real
    # creation always sets it explicitly via services.auth.add_crew_member.
    color: str = CREW_COLOR_PALETTE[0]


class SSHConnection(BaseModel):
    """How gaca-api reaches the Hermes box's own shell — to provision new
    crew members (`hermes profile create`, choosing a port, installing the
    gateway service) rather than requiring the user to do that by hand and
    re-type the result. Optional, and a materially bigger grant than any
    single `CrewMember.api_key`: that key only reaches one scoped Hermes
    profile's HTTP gateway, this can run arbitrary commands on the box.
    Never used for chat/delegate — only provisioning/discovery.
    """

    host: str  # bare hostname/IP — no scheme, no port (contrast agent_host)
    port: int = 22
    username: str
    private_key: str  # PEM, plaintext in `doc` like every other credential here


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

    # Each operator brings their own Hermes agent — there is no shared,
    # app-wide one. `agent_host` is the scheme and host that agent (and every
    # crew member under it) lives on, e.g. "http://david-claw", no port; each
    # CrewMember supplies its own port and key. Unset until the user connects
    # via POST /api/agent/connect, which also creates the first crew member;
    # every delegate/chat call until then raises CrewNotConfigured. Stored
    # plaintext in `doc` like the rest of this model's non-password fields —
    # no weaker than the single shared GACA_AGENT_API_KEY this replaces,
    # which also lived in plaintext in an env file.
    agent_host: str | None = None
    crew: list[CrewMember] = Field(default_factory=list)
    # Which crew member a delegate/session uses when nothing more specific
    # applies (no explicit override, and the alert's own `crew` doesn't match
    # a configured name). Cleared automatically if the member it names is
    # removed.
    default_crew: str | None = None
    # Optional — see SSHConnection. Unset until PUT /api/agent/provisioning; nothing
    # about connecting the agent (POST /api/agent/connect) requires it.
    ssh: SSHConnection | None = None

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_agent_fields(cls, data):
        """Read rows written under an earlier shape of this model.

        `agent_host` has actually had two names in its short life: the
        original 2026-08-22 multi-tenant retrofit called it `agent_host`; a
        2026-09-20 pass briefly renamed it to `crew_host` (reverted the same
        day once it became clear "which agent to connect to" and "which crew
        member to talk to" are different questions with different answers —
        see build-plan.md §11/§12). So: `crew_host`, if present with no
        `agent_host` alongside it, maps straight across. Separately, the
        2026-08-22 shape's `agents` list and `default_agent` field became
        `crew` and `default_crew` on 2026-09-20 and stayed that way — that
        rename stuck, only the host field name churned. Legacy `agents`
        entries predate `color` too, so they're backfilled from the palette
        by position. Old keys are left in the stored JSON, unread but
        harmless, until the row is next saved under the current ones.
        """
        if not isinstance(data, dict):
            return data
        if "agent_host" not in data and "crew_host" in data:
            data["agent_host"] = data["crew_host"]
        if "default_crew" not in data and "default_agent" in data:
            data["default_crew"] = data["default_agent"]
        if "crew" not in data and "agents" in data:
            legacy = data["agents"]
            if isinstance(legacy, list):
                for index, member in enumerate(legacy):
                    if isinstance(member, dict) and "color" not in member:
                        member["color"] = CREW_COLOR_PALETTE[
                            index % len(CREW_COLOR_PALETTE)
                        ]
            data["crew"] = legacy
        return data
