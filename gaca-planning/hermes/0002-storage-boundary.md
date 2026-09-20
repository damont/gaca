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
