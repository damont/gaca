# 0002 — What gaca-api stores vs. what it always reads live from Hermes

**Status:** Accepted · **Date:** 2026-09-20 · **Area:** gaca-api ↔ Hermes boundary (crew, skills, models)

## Context

The "crew" build (renaming the multi-profile concept, fixing chat, adding
skill insight) needed an explicit answer to a question that had only ever
been decided ad hoc, call by call: when gaca-api knows something about a
crew member's Hermes, does it keep its own copy, or does it ask Hermes fresh
every time? Getting this wrong in either direction has a real cost —
persisting something that drifts (a stale skill list the user trusts as
current) is worse than the extra network call it would have taken to avoid
it; refusing to persist something genuinely static (which port a crew member
runs on) would mean re-deriving it from nowhere on every request.

This isn't new territory — ADR 0001 already drew the line for model/provider
config ("per-deployment runtime configuration... through the API"). This ADR
generalizes that into one rule and writes down the crew/skills instance of
it, since it's now a live, shipped example rather than a plan.

## Decision

**One line: gaca-api stores how to reach a crew member, never what that
crew member can currently do.**

Two categories, and every new fact about a crew member has to land in one of
them — there is no third "cache it and refresh occasionally" option (see
Consequences for why).

### 1. Owned by gaca-api — a real row in its SQLite, the source of truth

- User accounts and auth.
- Agent/crew identity and addressing: `agent_host` (the one connected
  agent's box), and each `CrewMember`'s `name`, `port`, `api_key`, `color`,
  plus which one is `default_crew`. This is metadata Hermes has no concept
  of at all (a color) or has no reason to know about itself (which port an
  operator chose to run it on, from the gaca-api side) — it only exists
  because gaca-api needs it to make the next HTTP call.
- Alerts (the whole inbox and its triage state).
- Sessions, messages, and events — the whole chat transcript. Deliberate,
  and older than this ADR: the Hermes adapter's "Option A" replays full
  history to Hermes on every run rather than leaning on Hermes' own native
  session/memory concept (`gateway/platforms/api_server.py` does have one —
  `/api/sessions/*`, its own chat/streaming — unused by gaca-api on purpose),
  specifically so the transcript has exactly one home and survives a crew
  member being swapped, removed, or re-pointed at a different Hermes box.
- Push tokens.

### 2. Never persisted — always a live call to that crew member's own Hermes

- Skills (`GET /v1/skills`, wired via `AgentBackend.list_skills()` →
  `GET /api/crew/{name}/skills`).
- Models and provider metadata (`list_models` / `get_model_info`, reserved
  on the `AgentBackend` port per ADR 0001, not built yet — same rule applies
  whenever they are).
- Anything else describing a crew member's *capability* rather than its
  *identity*.

## Why this split

Identity/addressing changes rarely, is meaningless to anyone but gaca-api
(nobody else needs to know a crew member's port), and gaca-api is the only
thing that could ever produce it in the first place — a textbook database
row. Capability is Hermes' own mutable state: an operator can add or disable
a `SKILL.md`, or (once built) switch a model, with zero notification to
gaca-api. There is no cache-invalidation story for that which doesn't
eventually reduce to "just ask Hermes" — so skip the cache and ask Hermes.

## Consequences

- **A new "what can this crew member do" feature defaults to a live call,
  not a new persisted mirror.** Toolsets, cron jobs, or anything similar
  Hermes' gateway exposes read-only should follow the skills pattern
  (`AgentBackend` method → adapter → thin router passthrough), not grow a
  table.
- **If a future feature genuinely needs to cache capability** (a real
  latency or availability argument, not just convenience), that's an
  exception to argue and record here explicitly — not a default to drift
  into silently.
- **This does not change how alerts/sessions work** — those were already
  squarely in category 1 before this ADR; it just names the boundary they
  were already on the right side of, so the next ambiguous case has a rule
  to check against instead of being decided fresh.

## Related

- `0001-runtime-model-and-provider-config.md` — the model/provider instance
  of this same rule, decided first.
- `../alerts/build-plan.md` §11 — the crew rename + skills build this ADR
  came out of.
- `../hermes-plan.html` — "Locked decisions" carries a pointer back here.

## Exception (2026-10-03): a crew member's purpose is read live over SSH

**What:** `GET /api/crew/{name}/profile` includes `purpose` — what the crew member is
dedicated to doing (Hermes: the `description` in `profile.yaml` and the persona in `SOUL.md`).

**Why it's an exception, twice over.** It is a capability-style fact, so by the rule above it is a
live read, never stored — and it is: no table, no cache, read fresh each time. But the rule's
default transport is Hermes' HTTP gateway, and Hermes does not serve this there. The only HTTP
route is the dashboard's `GET /api/profiles/{name}/soul`, and the dashboard is bound to
`127.0.0.1` and authenticated, and it also fronts secrets and terminals — not something to expose
to gaca-api. So the read goes over the SSH connection gaca-api already holds, which
`provisioning/ssh.py` said was "only to provision or discover crew members".

**How the exception is kept small:**
- one fixed, read-only command (`ssh.read_profile_files`) — the profile name is the only input, it
  is validated against a strict pattern on both ends, and it can only select a directory directly
  under `~/.hermes/profiles/`;
- it reads two small files (capped at 40 KB and 4 KB) and nothing else;
- failure is soft: no SSH access configured, unreachable box, or a timeout leaves `purpose: null`
  and lists `"purpose"` in `unavailable`; the rest of the profile still loads;
- the shape of the answer is GACA's (`ProfilePurpose`, `schemas/crew_profile.py`); the Hermes
  vocabulary and Hermes' stock "general assistant" text are confined to
  `agent/hermes/translate_purpose.py`.

**Still not allowed:** using this SSH credential on the chat or delegate paths, or caching what it
returns. If purpose ever needs to be fast or available without SSH, the right fix is for Hermes to
serve it from its gateway, not for gaca-api to keep a copy.

## Addendum (2026-10-03): a crew member's address follows Hermes' gateway layout

`CrewMember.port` used to be the whole address, because Hermes ran one gateway process per profile,
each on its own port. Hermes v0.21 changed that — **one gateway per host now serves every profile**
(its "multiplex-only" ruling; per-profile gateways survive only as a temporary
`gateway.standalone: true` shim) — and removed the per-profile gateways. Every non-default crew member
became unreachable until gaca-api learned the new layout.

So the address is now `agent_host:port` **plus an optional `path`**: `""` for a gateway of its own
(and for the default profile), `/p/<name>` for a profile on the shared gateway. It stays in the
"owned by gaca-api — how to reach a crew member" category above: it is addressing, not capability.
Each profile keeps its own `api_key` on the shared gateway.

- **Hermes doesn't say which layout it's running**, so discovery asks: if the default gateway answers
  `GET /p/<name>/health`, that profile is served there. (`provisioning/ssh.py`)
- **`POST /crew/discover` repairs moved members**: a known member whose key matches what the box reports
  but whose address differs is updated. A key mismatch is never touched, so it can't clobber a
  hand-entered row. Re-running discovery after any Hermes upgrade is the recovery step.
- **Provisioning follows the installed Hermes**: on a shared gateway there is nothing to install — it
  waits for `/p/<name>` to come up (the gateway rescans about every 30s) and records that address; on an
  older Hermes it installs a gateway of its own as before.
- `path` is validated (every segment starts with a letter or digit, so `..` can't be written) at the
  request layer and on the model.
