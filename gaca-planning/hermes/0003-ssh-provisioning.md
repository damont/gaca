# 0003 — gaca-api may hold SSH access to provision crew members directly

**Status:** Accepted (Slice A only — see Consequences) · **Date:** 2026-09-20 · **Area:** gaca-api ↔ Hermes box, crew provisioning

## Context

Every crew member so far has required the user to do two things by hand,
outside GACA entirely: run `hermes profile create <name>` on the Hermes box,
and pick a free port for its gateway. GACA only ever recorded the result.
The user wants GACA to do that work itself — create the profile and choose
the port — rather than being told about it after the fact.

Hermes has a real, first-class multi-profile mechanism for this
(`hermes profile create <name>`, `hermes gateway install --profile <name>`,
service-supervisor-backed, confirmed by reading Hermes' own CLI source —
`hermes_cli/profiles.py`, `hermes_cli/service_manager.py`). None of it is
reachable over HTTP; it's local CLI/filesystem/service-supervisor operations
on the box itself. So gaca-api needs *some* way to execute commands there —
it has never had that before; until now it was purely an HTTP client to
Hermes' own gateway API (see 0001, 0002).

Three shapes were on the table:

1. **SSH from gaca-api directly to the box.** Simplest to build — no new
   component — but gaca-api now holds a credential that can run anything on
   the box, not a scoped API call.
2. **A small companion service on the box**, exposing a narrow HTTP surface
   (`POST /profiles`, `GET /profiles`) that wraps the CLI calls. Narrower
   blast radius, matches the "narrow explicit surface" pattern already used
   elsewhere in this project (Hermes' own skill-scoped tokens) — but is a
   new thing to build and keep running.
3. **Discovery only, no remote creation** — the user keeps creating
   profiles by hand; gaca-api just learns to read what already exists.
   Smaller step, doesn't address "the API should create them."

## Decision

**(1), chosen explicitly by the user, aware of the trade-off in (2).**
`UserDoc.ssh: SSHConnection | None` — host, port, username, and a private
key, optional and separate from `agent_host`/`CrewMember.api_key`. Never
used to serve a chat/delegate call, only provisioning/discovery.
`gaca_api/provisioning/ssh.py` wraps `asyncssh`, structurally parallel to
`agent/<engine>/adapter.py` but for a fundamentally different kind of
access — a shell, not a scoped gateway.

**Host key verification is off** (`known_hosts=None`) — there's no
trust-on-first-use store to pin a box's key against yet. Accepted as a known
gap for a personal box on a private network; revisit before this reaches
anything less trusted.

**SSH is internal, not part of the API contract.** First pass exposed
`/agent/ssh`, `SSHConnectionSet`, etc. directly — wrong, caught immediately:
"ssh is just the implementation detail, the caller should have zero idea
we're using it," same rule already applied to `port`/`process` in
`models/user.py`'s `CrewMember` docstring, just not applied to myself here.
Corrected to `/agent/provisioning`, `ProvisioningAccess`,
`ProvisioningStatus`, `ProvisioningTestResult` — the wire contract says only
"gaca-api can set up crew members on this box for you," never "ssh." Only
`models/user.py`'s `SSHConnection` and `gaca_api/provisioning/ssh.py` stay
honestly SSH-named, because that layer — like `agent/hermes/adapter.py` — is
genuinely, permanently about one specific mechanism; the boundary is the
router, same place `crew_out()` already translates internal field names to
public ones.

## Consequences

- **This ADR covers Slice A only**: storing the access credential
  (`PUT /api/agent/provisioning`) and proving it works
  (`POST /api/agent/provisioning/test`, which runs a harmless command and
  reports pass/fail). Tested against a real local SSH server (`asyncssh`'s
  own), not mocks.
- **Slice B — actually running `hermes profile create` / `gateway install`
  to provision a crew member — is deliberately NOT built yet.** The exact
  command syntax (flags for naming a profile, setting its port, installing
  the gateway service) needs verifying against the user's *live* Hermes CLI
  before anything executes real commands against a real box — the nix-store
  pin has already been wrong once for this project (see
  gaca-nix-hermes-progress memory / README). Guessing flags for a command
  that mutates a personal server is a different risk class than guessing an
  HTTP response shape.
- **Port selection** (once Slice B exists): the plan is to ask the box what
  it already has running (via SSH, `list_profile_gateways()` or equivalent)
  rather than just incrementing from what gaca-api's own database happens
  to know — the database can be wrong or stale, the box is the actual truth.
- **Discovery** (reading pre-existing profiles the user set up by hand, not
  through GACA) becomes straightforward once Slice B's SSH access exists —
  same mechanism, read-only. Not built yet either.
- If option (2) — the companion service — ever looks more attractive later
  (e.g. after seeing how much surface Slice B actually needs), that's a
  reasonable reason to revisit this ADR, not a sign it was wrong to start
  with (1).

## Related

- `0001-runtime-model-and-provider-config.md`, `0002-storage-boundary.md` —
  the identity/config-vs-capability boundary this extends: SSH access is
  identity/config too (how to reach the box), just a much bigger grant than
  a `CrewMember.api_key`.
- `../alerts/build-plan.md` — the crew rename and agent/crew split this
  followed directly from.
