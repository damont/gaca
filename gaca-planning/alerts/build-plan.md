# GACA Alerts — build plan

**Status:** Agreed, not started · **Date:** 2026-08-07 · **Branch:** `feature/hermes-alerts`
**Area:** gaca-api ↔ Hermes ↔ Android

The alerting capability: Hermes notices something, posts an **Alert** to gaca-api,
and the Android app shows it so David can triage it into one of four dispositions —
hand it back to Hermes, act on it himself, ignore it, or defer it.

This document is the whole build. The Android-facing HTTP contract is split out
into [`api-contract.md`](api-contract.md) so the app can be built against it
independently.

---

## 1. Terminology — Alert is not Event

`gaca-api` **already** has a thing called `Event`, and it is a completely
different animal. Getting these confused will cost hours, so:

| | `Event` (`GacaEvent`) — exists today | `Alert` — what we're building |
|---|---|---|
| **What** | One frame of a live agent run | A durable "you should know about this" |
| **Lifetime** | Seconds. The duration of one run | Until triaged. Days, maybe |
| **Who emits** | The agent engine, via `agent/hermes/translate.py` | Hermes, deliberately, via HTTP |
| **Variants** | `assistant.delta`, `assistant.reasoning`, `tool.started`, `tool.completed`, `run.completed`, `run.failed` | One shape, many `kind` values |
| **Storage** | `events` table, `seq INTEGER PRIMARY KEY AUTOINCREMENT` | `alerts` table, `id TEXT PRIMARY KEY` |
| **Transport** | SSE, resumable by `Last-Event-ID` | REST + a separate SSE stream |
| **Purpose** | Make a chat reply render progressively | Be the inbox |

We considered renaming the streaming one to `RunEvent` and decided against it.
**Both names stay.** Everything under `schemas/event.py`, `db/events.py`,
`services/events.py` and the `events` table is the *streaming* concept and is not
touched by this build.

---

## 2. Topology

A dedicated Raspberry Pi (**gaca-pi**) hosts the API. Hermes reaches it *only*
through a Cloudflare Tunnel, authenticated with a JWT. gaca-pi reaches the Hermes
box outbound. Hermes has no route back.

```
  Android app ──┐
   (session JWT)│
                ├──► Cloudflare Tunnel  ──┐   outbound-initiated:
  Hermes ───────┘    gaca.<yourdomain>    │   NO inbound port opens
   (agent JWT,                            │
    outbound HTTPS only)                  ▼
                  ┌──────────────────────────────────────────┐
                  │ gaca-pi          ufw: default deny in    │
                  │                                          │
                  │  cloudflared ──► nginx:80                │
                  │                   /     → React SPA      │
                  │                   /api/ → gaca-api:41700 │
                  │                            └─ SQLite     │
                  └──────────────────┬───────────────────────┘
                                     │  outbound only
                     ┌───────────────┴────────────────┐
                     │ SSH  (read files, run commands)│
                     │ HTTP → Hermes gateway :8642    │
                     ▼
                  ┌─────────────────────────────┐
                  │ Hermes box                  │
                  │ ✗ no route into gaca-pi     │
                  └─────────────────────────────┘
```

**Why this is safe:** a Cloudflare Tunnel is outbound-initiated. `cloudflared`
dials *out*; nothing dials in. So gaca-pi runs `ufw default deny incoming` with
nothing open but your own SSH, and still serves a public API. Hermes sitting on
the same LAN can ping gaca-pi and find every port closed. The only door is the
public hostname, and that needs a JWT.

`GACA_API_HOST` already defaults to `127.0.0.1`, which is correct here — the API
and nginx both stay on loopback.

**Three channels:**

| Direction | Transport | Auth | Used for |
|---|---|---|---|
| Hermes → API | Cloudflare, HTTPS | agent JWT | `POST /api/alerts` — Hermes' main write |
| API → Hermes | private link, HTTP `:8642` | `GACA_AGENT_API_KEY` | delegations + chat (existing `HermesAdapter`) |
| API → Hermes box | private link, SSH | key | reading Hermes files, diagnostics |

**Because the API can reach Hermes directly, delegation is just the existing
push path.** No work queue, no polling, no new adapter. `POST /api/alerts/{id}/delegate`
calls into `services/runs.py` exactly like a chat turn does.

**Risk to close:** gaca-pi's SSH key gives full access to the Hermes box, and
gaca-api is internet-reachable. If the API is compromised, that key is the pivot.
Use a dedicated `gaca-reader` user on the Hermes box, or `command="…"` restrictions
in its `authorized_keys` — not root with a bare key.

---

## 3. The Alert model

### Fields

| Field | Type | Notes |
|---|---|---|
| `id` | `str` | `new_id()` from `models/base.py` |
| `user_id` | `str` | owner-scoped from day one |
| `created_at` | `datetime` | UTC |
| `source` | `str` | label of the agent key that posted it — tells Hermes from a future poster |
| `kind` | `str` | free-form dotted string: `email.flagged`, `disk.low`, `cal.conflict`. **Not an enum** — Hermes must be able to invent a kind without an API deploy |
| `severity` | `info \| warn \| urgent` | drives how loud the Android app is |
| `title` | `str` | one line |
| `body` | `str \| None` | longer, markdown |
| `data` | `dict` | arbitrary JSON — the raw thing Hermes saw |
| `dedupe_key` | `str \| None` | optional; re-posting the same key **updates** instead of creating |
| `suggested_actions` | `list[SuggestedAction]` | `[{label, prompt}]` |
| `status` | see §4 | |
| `status_changed_at` | `datetime` | |
| `snooze_until` | `datetime \| None` | set when snoozed |
| `session_id` | `str \| None` | set when delegated — links to the chat session running it |
| `resolution_note` | `str \| None` | how it ended, or why it failed |
| `parent_alert_id` | `str \| None` | for `gaca.clarify` children (§9) |

### Two fields that carry weight

**`dedupe_key`** — without it, a Hermes cron checking disk every 5 minutes gives
you 288 identical alerts a day. This is the single most important field for the
app being usable. Enforced with a partial unique index:

```sql
CREATE UNIQUE INDEX IF NOT EXISTS ux_alerts_dedupe
    ON alerts (user_id, dedupe_key) WHERE dedupe_key IS NOT NULL;
```

`POST /api/alerts` with an existing `(user_id, dedupe_key)` updates the row and
resets `status` to `new` — a re-fired condition should resurface even if you
dismissed it last time.

**`suggested_actions`** — Hermes posts *"Disk 91% full"* with a button labeled
*"Clean up old logs"* carrying the exact prompt to hand back. The app renders
buttons instead of a text box. This is what makes it feel like an assistant
rather than an inbox.

### Schema additions to `db/client.py`

```sql
CREATE TABLE IF NOT EXISTS users (
    id         TEXT PRIMARY KEY,
    email      TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    doc        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    status      TEXT NOT NULL,
    kind        TEXT NOT NULL,
    severity    TEXT NOT NULL,
    dedupe_key  TEXT,
    doc         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_alerts_inbox
    ON alerts (user_id, status, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS ux_alerts_dedupe
    ON alerts (user_id, dedupe_key) WHERE dedupe_key IS NOT NULL;
```

Follows the existing storage shape: indexed key columns for querying plus a `doc`
column holding the Pydantic model as JSON.

---

## 4. State machine

```
                  ┌──────────────────────────► dismissed   (ignored, terminal)
                  │
  [hermes posts] ─┼─► new ─┬─────────────────► acted       (done, terminal)
                  │        │                      ▲
                  │        ├─► snoozed ───────────┼──┐ (snooze_until passes → new)
                  │        │                      │
                  │        └─► delegated ─────────┘  (Hermes finished)
                  │                  │
                  └──────────────────┘  (Hermes failed → back to new, error attached)
```

Five states. Four of them are David's four choices; `new` is the inbox.

**Three design calls:**

1. **`delegated` resolves into `acted`, not its own terminal state.** "Acted"
   means *something got done* — by you or by Hermes. Five states instead of seven.

2. **A failed Hermes run bounces to `new`** with the failure in `resolution_note`.
   A delegation can never silently vanish.

3. **Snooze needs no background worker.** Rather than a sweeper flipping
   `snoozed → new` on a timer, the list query computes it:

   ```
   effective_status = 'new'  if status == 'snoozed' and snooze_until <= now
                    else status
   ```

   No Redis, no cron, no worker process. Matches the build skill's
   "background workers: none" default. The stored `status` stays `snoozed`;
   only the *view* changes.

---

## 5. Conventions

Where this build follows the `build` skill and where it deliberately doesn't.

| | Skill says | This build | Why |
|---|---|---|---|
| Layout | `api/` + `frontend/` | keep `gaca-api/gaca_api/` (routers·services·db·models·schemas); add `gaca-web/` | the layering was built deliberately; see [[gaca-api-layering]] |
| Database | MongoDB + Beanie | **SQLite + aiosqlite** | right answer on a Pi; one file, backups are a file copy |
| Route prefix | `/api/…` | **adopt** — move `/sessions` too | phase-08 nginx proxying depends on it |
| Auth | JWT + Argon2, email+password | **adopt as-is** | |
| Agent keys | phase 10 long-lived JWT | **adopt as-is** | chosen over revocable DB keys |
| Ports | api 8020, web 8095 | **41700 / 41800** | 8020/8095 likely collide with other Pi apps |
| Config | bare `JWT_SECRET` | keep the **`GACA_`** prefix | consistency inside this app |
| Deploy | Path A: Pi + Cloudflare Tunnel | **adopt** | |
| Workers | none | **none** | snooze is computed, not swept |

**On phase-10 JWTs:** they can't be revoked individually. Mint Hermes' token at
~90 days rather than 365 so a leak has bounded life. Break-glass is rotating
`GACA_JWT_SECRET`, which logs everyone out.

**Nix is not in the loop.** `flake.nix` stays at the repo root (it's how Hermes
was installed) but gaca-api runs as a plain `uv` project and later Docker.

---

## 6. Slices

Each slice is independently runnable and ends with something testable.
Per the working agreement: Claude writes and explains, **David runs the commands**.

### Slice 0 — Plain `uv` run path, routes under `/api`

**Goal:** confirm we're genuinely un-Nixed, and move the URL surface before
anything points at it.

- Verify `uv sync` + `uv run uvicorn gaca_api.main:app --port 41700 --reload`
- `routers/sessions.py`: `APIRouter(prefix="/sessions")` → `prefix="/api/sessions"`
- `main.py`: `/health` → `/api/health`
- Update `scripts/dev.sh` and `README.md` to the new paths

**Done when:** `gaca-new` / `gaca-listen` / `gaca-say` still work end-to-end.

> Cheapest now. After there's a frontend and an Android app pointed at old paths,
> it's a coordinated change across three codebases.

---

### Slice 1 — Users + email/password auth

**Goal:** a real user to own alerts.

- `pyproject.toml`: add `PyJWT`, `argon2-cffi`, `pydantic[email]`
- `config.py`: `jwt_secret`, `jwt_algorithm` (`HS256`), `jwt_expire_minutes` (10080)
- `db/client.py`: `users` table
- `models/user.py` — `UserDoc(id, email, display_name, hashed_password, is_active, created_at)`
- `schemas/auth.py` — `RegisterRequest`, `LoginRequest`, `TokenResponse`, `UserResponse`
- `db/users.py`, `services/auth.py`, `utils/auth.py` (hash/verify, `create_access_token`, `get_current_user`)
- `routers/auth.py` — `POST /api/auth/register`, `POST /api/auth/login`, `GET /api/auth/me`
- Tests: `tests/services/test_auth.py`

**Deviation from skill:** Argon2 not bcrypt (skill says bcrypt 5.x has
compatibility problems). Password reset / SMTP is **deferred** — single-user for
now, and it needs an SMTP decision we don't need to make yet.

**Done when:** register → login → `GET /api/auth/me` with the Bearer token.

---

### Slice 2 — Agent tokens + OpenAPI exposure (build skill phase 10)

**Goal:** a long-lived JWT to hand Hermes, and a discoverable API.

- `utils/auth.py`: `create_access_token(subject, expires_delta=None)`
- `schemas/auth.py`: `AgentTokenRequest(expires_in_days: int = 30, ge=1, le=365)`, `AgentTokenResponse`
- `routers/auth.py`: `POST /api/auth/agent-token`, protected by `Depends(get_current_user)`
- `main.py`: `docs_url="/api/agent"`, `openapi_url="/api/openapi.json"`, `redoc_url=None`, plus `GET /api/schema`

No password re-prompt — the caller already holds a session JWT. Per the skill:
if a session JWT is stolen the attacker can already do anything the user can,
so demanding a password to mint a longer token adds friction without changing
the threat model.

**Done when:** `POST /api/auth/agent-token` returns a JWT that `get_current_user`
accepts, and `/api/openapi.json` serves a schema.

---

### Slice 3 — Alert model, create and read

**Goal:** Hermes could post; you could list.

- `db/client.py`: `alerts` table + both indexes
- `models/alert.py` — `AlertDoc`, `SuggestedAction`
- `schemas/alert.py` — `AlertCreate`, `AlertOut`, `AlertList`
- `db/alerts.py` — `insert`, `upsert_by_dedupe_key`, `find_by_id`, `find_by_user`
- `services/alerts.py` — create (with dedupe), list (with **effective status**), get
- `routers/alerts.py` — `POST /api/alerts`, `GET /api/alerts`, `GET /api/alerts/{id}`
- `main.py`: include the router
- Tests: `tests/services/test_alerts.py` — **must cover the dedupe path**

**Done when:** posting the same `dedupe_key` twice yields one row with updated
content, and a snoozed alert whose `snooze_until` has passed appears as `new`
in the list.

---

### Slice 4 — Triage transitions

**Goal:** the full state machine.

- `services/alerts.py`: `act`, `dismiss`, `snooze`, and the transition guard
- `routers/alerts.py`: `POST /api/alerts/{id}/act`, `/dismiss`, `/snooze`
- `services/exceptions.py`: add `InvalidTransition` → 409 handler in `main.py`
- Tests: every legal transition, plus rejection of illegal ones

`delegate` lands in slice 6 — it needs the agent runtime, and everything else
here is pure state.

**Done when:** all four dispositions work by curl and illegal transitions 409.

---

### Slice 5 — The Hermes skill · ⭐ first end-to-end

**Goal:** Hermes actually posts an alert you can see.

Canonical copy lives in the repo at `gaca-planning/alerts/hermes-skill/SKILL.md`;
installed to `~/.hermes/skills/gaca/gaca-alerts/SKILL.md`.

**Format — copy the live install, not the bundled skills.** David's Hermes is
**not** version 0.18, so anything read out of `/nix/store/*hermes-agent-0.18.0*`
is unreliable. Template off `~/.hermes/skills/research/web-research-guide/`,
which is writable and real on his box:

```yaml
---
name: gaca-alerts
title: "GACA Alerts — tell David about things"
description: "Post alerts to GACA when you notice something David should know
  about or decide on. Covers the alert schema, severity, dedupe discipline, and
  suggested actions."
tags: [gaca, alerts, notifications, http]
trigger: "You notice something David should know about or decide on, or he asks
  what's in his GACA inbox"
---
```

Note this is *flatter* than the bundled-skill frontmatter — top-level `tags`,
no `version`/`platforms`/`metadata.hermes`, plus a `trigger` field that is the
explicit "when should I load this" hint.

**Contents:** the phase-10 `agentSkill()` template gives you the bones — what the
app is, the two things to ask the operator for, the `Authorization: Bearer` rule,
"fetch `/api/openapi.json` rather than assuming endpoints", and the failure-mode
table. **On top of that**, the part that actually matters, because it can't be
expressed in an OpenAPI schema:

- when is something `urgent` vs `warn` vs `info`
- what makes a good `dedupe_key` — the difference between 1 alert and 288
- when to attach `suggested_actions`, and how to write a prompt that comes back and works
- **don't post an alert for something you should just fix**

**Three cautions:**

1. **The skill curator is live and unpaused.** `~/.hermes/skills/.curator_state`
   shows it ran 2026-07-14 and keeps tarball backups. Today it's benign
   (`"auto: no changes; llm: skipped (consolidation off)"`) but if consolidation
   is ever switched on, a hand-written skill could be rewritten or merged.
   **The repo copy is canonical; the installed copy is disposable.**

2. **Install as a new top-level category.** Bundled categories are read-only
   nix store paths. `~/.hermes/skills/gaca/` can't collide and survives an update.

3. **The JWT never goes in `SKILL.md`** — a skill body is exactly the kind of file
   a curator copies around. It goes in `~/.hermes/.env`, which already holds
   `API_SERVER_KEY` and `TAVILY_API_KEY`. The skill names the variable.

**Why a skill and not a plugin or MCP:** Hermes already has HTTP tooling; what it
lacks is *knowledge*. A skill is a file — no restart, no rebuild, and it travels
to a different box easily. If alert quality slips, the escalation is **MCP**, not
a plugin: gaca-api already generates the JSON Schema from Pydantic, an MCP router
lives in the same app on the same deploy, and it stays engine-neutral per the
`AgentBackend` port in `agent/base.py`. A Hermes plugin would be engine lock-in —
the exact thing that port exists to prevent.

**Done when:** Hermes, told "let David know the disk is filling up", posts a
well-formed alert that appears in `GET /api/alerts`. Works entirely on localhost
at this stage; only the base URL changes at slice 10.

---

### Slice 6 — Delegation

**Goal:** hand an alert back to Hermes.

- `services/alerts.py`: `delegate(alert_id, instruction)` — creates a session,
  builds the prompt from the alert (+ chosen `suggested_action`), calls
  `services/runs.py:start_turn`, stores `session_id`, sets `status=delegated`
- Watch `run.completed` / `run.failed` to settle the alert to `acted` or bounce
  it to `new` with the error
- `routers/alerts.py`: `POST /api/alerts/{id}/delegate`

**Done when:** delegating an alert starts a real Hermes run, streaming Events
arrive on the linked session, and the alert settles to `acted`.

---

### Slice 7 — React frontend

**Goal:** sign up, log in, mint a key.

`gaca-web/` — Vite + React 19 + TS + Tailwind v4, dev port 41800 proxying
`/api` → 41700.

**This is 4–5 teaching sub-slices, not one.** David is a backend dev new to
React; the frontend track started with `gaca-test-ui/index.html` (a plain HTML
page with one DOM write) on purpose. Sub-slices get broken out when we reach
this point — front-loading them now would be guessing at the right pace.

Scope is deliberately small: **signup, login, and the agent-token screen.** Alert
triage lives in the Android app. The web UI exists to get a key.

Includes phase 10's `lib/agentSkill.ts` + `agentSkill.test.ts` — the "Download
skill" button generates the markdown you hand Hermes alongside the token.

**Done when:** sign up in a browser, mint a token, download the skill file.

---

### Slice 8 — SSE alert stream

**Goal:** live inbox for Android.

- `services/alerts.py`: publish to `bus.py` on every create and transition
- `routers/alerts.py`: `GET /api/alerts/stream`

`bus.py` is already shaped like Redis pub/sub and already carries serialized
JSON strings, so this is mostly reuse. **Channel key is `user_id`**, not
`session_id` — a different channel namespace from the run stream.

**Done when:** an alert posted by Hermes appears on an open stream without a poll.

---

### Slice 9 — Docker

**Goal:** `docker compose up --build`.

Multi-stage `Dockerfile` (backend / frontend-build / nginx), `docker-compose.yml`,
`gaca-web/nginx.conf` with the SPA fallback and the `/api/` proxy.

**SQLite needs a volume** — the skill's compose assumes MongoDB on the host and
has no volume for app data. Mount `/var/lib/gaca` and set
`GACA_DB_PATH=/var/lib/gaca/gaca.db`, or the database dies with the container.

**Done when:** the full stack runs from compose on the dev box.

---

### Slice 10 — gaca-pi provisioning

**Goal:** the topology in §2, live.

- Docker + compose on the Pi
- `cloudflared` → `gaca.<yourdomain>` → `nginx:80`
- `ufw default deny incoming`; allow only your own SSH
- Restricted SSH key from gaca-pi → Hermes box (`gaca-reader` user or `command=`)
- Hermes' `~/.hermes/.env` gets the agent JWT; the skill's base URL flips from
  localhost to the public hostname

**Prerequisite:** confirm gaca-pi and the Hermes box share a private network. If
Hermes lives on a machine that moves, this needs Tailscale or WireGuard instead
of plain LAN — same design, different plumbing.

**Done when:** Hermes posts through Cloudflare, and gaca-pi's ports are closed
from the LAN.

---

### Slice 11 — Hermes cron

**Goal:** the loop runs itself.

Hermes has a built-in scheduler (`~/.hermes/cron/`, with a ticker heartbeat and
jobs lock) and — on 0.18 at least — a `cronjob_tools.py` that lets the agent
create and manage its own jobs *as a tool call*. If that holds on David's
version, this slice is a conversation, not a config edit: *"check disk daily and
alert me above 85%"* and Hermes schedules itself.

**Verify against the live install first** — this is one of the things read from
the 0.18 store path.

**Done when:** a recurring check posts an alert with no human in the loop.

---

## 7. Config additions

All `GACA_`-prefixed, in `config.py` and `.env.example`:

| Var | Default | Notes |
|---|---|---|
| `GACA_JWT_SECRET` | `change-me` | **must** be set in production |
| `GACA_JWT_ALGORITHM` | `HS256` | |
| `GACA_JWT_EXPIRE_MINUTES` | `10080` | 7 days, browser sessions |
| `GACA_FRONTEND_BASE_URL` | `http://localhost:41800` | |

**Remove `GACA_API_KEY`** — the unused single-user placeholder. Real auth
replaces it in slice 1.

---

## 8. Test strategy

`tests/` already has `test_bus.py`, `tests/services/`, `tests/agent/`. Extend the
same shape. Per slice, the tests that would actually catch a regression:

| Slice | Must cover |
|---|---|
| 1 | wrong password rejected; expired token rejected; `/me` returns the right user |
| 2 | agent token accepted by `get_current_user`; `expires_in_days` bounds enforced |
| 3 | **dedupe upsert**; effective-status computation for a passed snooze; user scoping |
| 4 | every legal transition; illegal transitions 409 |
| 6 | `run.failed` bounces the alert to `new` with the error recorded |
| 8 | an alert published mid-stream reaches an attached subscriber |

The dedupe and effective-status tests are the two worth writing carefully —
they're the logic that isn't obvious from reading the code.

---

## 9. Open questions

**`gaca.clarify` child alerts.** Hermes has a `clarify_tool` (on 0.18) that lets
it ask a question mid-run. The state machine has no answer for that: you delegate,
Hermes gets halfway and needs a decision. It isn't `acted`, isn't failed, and
shouldn't sit in `delegated` forever.

Proposal — **no new state**. Hermes posts a *new* alert of kind `gaca.clarify`
with `parent_alert_id` set. The question lands in the same inbox, gets the same
triage, and your answer is a `delegate` on the child carrying the reply. The
clarification is durable instead of trapped in a run that might time out.

`parent_alert_id` is already in the model (§3) so the field is there when we
decide. **Not built until slice 6 proves it's needed.**

**Android push.** SSE only works while the app is open. Real push needs FCM.
`severity` is the seam. Out of scope for this build.

**Password reset.** Deferred from slice 1 — needs an SMTP decision, and single-user
doesn't need it yet.

---

## 10. What isn't in this build

- Renaming the streaming `Event` concept (decided against)
- A Hermes pull/work queue (unnecessary — the API can reach Hermes)
- A Hermes plugin or MCP server (skill first; MCP is the escalation)
- Revocable DB-backed agent keys (phase-10 JWTs chosen instead)
- Alert triage in the web UI (Android owns triage)
- FCM push, password reset, multi-tenant anything beyond `user_id` scoping
