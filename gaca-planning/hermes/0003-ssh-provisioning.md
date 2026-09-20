# 0003 — gaca-api may hold SSH access to provision crew members directly

**Status:** Accepted, fully built (Slices A and B) · **Date:** 2026-09-20 · **Area:** gaca-api ↔ Hermes box, crew provisioning

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

- **Slice A**: storing the access credential (`PUT /api/agent/provisioning`)
  and proving it works (`POST /api/agent/provisioning/test`, which runs a
  harmless command and reports pass/fail). Tested against a real local SSH
  server (`asyncssh`'s own), not mocks.
- **Discovery** (reading pre-existing profiles set up by hand, not through
  GACA): `GET`-style read via `POST /api/agent/discover`, reading each
  profile's own `.env` for its `API_SERVER_*` keys. Confirmed live against
  `david-claw`, 2026-09-20 — found the real `default`/`toolshed`/`trading`,
  each already running.
- **Slice B — `POST /api/crew` with `port`/`api_key` omitted now actually
  provisions a new profile.** The command sequence was verified against the
  *live* Hermes CLI on `david-claw` before writing any code against it —
  `hermes --help`/`profile --help`/`gateway --help`/`config --help`, then
  the real three-step sequence: `hermes profile create <name> --no-alias`
  (no `--port` flag exists — profile create never touches `API_SERVER_*`),
  upsert `API_SERVER_ENABLED`/`HOST`/`PORT`/`KEY` directly into that
  profile's own `.env` (`hermes config set` only touches `config.yaml`-style
  keys, confirmed via `--help`, not `.env`), then
  `hermes --profile <name> gateway install --start-now` (`--profile` is a
  real global flag, confirmed both by testing it directly and by Hermes' own
  error-log text suggesting it as the fix for a different problem). Every
  command wrapped in `bash -lc` because `hermes` lives in `~/.local/bin`,
  only on `PATH` in a login shell.
- **Validated for real, not just in tests**: ran `create_profile` against
  `david-claw` with an obviously-named throwaway profile
  (`gaca-validation-test`), confirmed it appeared in `hermes profile list`
  running, its `.env` had the exact values sent, and its HTTP gateway
  answered a real request on the assigned port (8645) — then deleted it
  (`hermes profile delete -y`) and confirmed full cleanup (service removed,
  directory gone, port stopped responding). The unit tests use a fake
  `hermes` CLI script (the real one isn't available in a sandbox) that
  mimics this exact sequence, not a stand-in for arbitrary behavior.
- **Port selection**: `next_free_port` asks the box what's already running
  (via the same mechanism `discover_profiles` uses) and picks one past the
  highest, rather than trusting gaca-api's own database, which can be stale
  or simply not know about a profile someone set up by hand.
- **Not addressed here**: a genuine port or profile-name race between two
  concurrent provisioning calls for the same account (unlikely — this is a
  personal app, one user at a time provisioning their own box — but a real
  gap, not silently handled). Revisit if this ever needs to be safe under
  concurrency.
- If the companion-service alternative from Context ever looks more
  attractive later (e.g. wanting a narrower blast radius than full SSH),
  that's a reasonable reason to revisit this ADR, not a sign it was wrong to
  start with SSH.

## Related

- `0001-runtime-model-and-provider-config.md`, `0002-storage-boundary.md` —
  the identity/config-vs-capability boundary this extends: SSH access is
  identity/config too (how to reach the box), just a much bigger grant than
  a `CrewMember.api_key`.
- `../alerts/build-plan.md` — the crew rename and agent/crew split this
  followed directly from.
