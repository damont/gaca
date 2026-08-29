# GACA Alerts — build plan

**Status:** Slices 0–5, 7, 8, 9, and 12 shipped — the real triage surface
exists now, not just `curl`. Hermes is genuinely posting and delegating real
alerts through the real deployment, not a stub (see slice 9 and
[`hermes-setup.html` Step 13](../hermes-setup.html#step13)). Google sign-in on
top of 5; slice 6 (SSE alert stream) **cut**; slice 10 (Delegation) shipped
early, out of numeric order — see its heading below for why. §10 (new, not in
the original slice list) retrofit per-user agent credentials + closed a real
auth gap in sessions, also shipped and deployed. **Slice 11 (Hermes cron) is a
deliberate skip, not a gap** — decided against, not scheduled. FCM push
notification work (`models/push_token.py`, `schemas/push.py`,
`db/client.py`'s `push_tokens` table, wired into `main.py`/`services/alerts.py`)
went in alongside 12, giving Android somewhere to register a device token
against. **This build is done** — everything remaining is enhancement, not a
missing slice. · **Date:** 2026-08-07, reordered 2026-08-09, updated
2026-08-23
**Branch:** `feature/hermes-alerts` · **Area:** gaca-api ↔ Hermes ↔ Android

**Ordering (revised 2026-08-09):** finish the app — frontend, Docker — then
deploy it to the Pi behind Cloudflare, *then* point Hermes at it,
*then* build the Android app. The Hermes work (originally slices 5–6) moved to
9–10. The reason is that everything before slice 8 can be built and verified on
the dev box with nothing else running, whereas every Hermes slice needs a second
machine, a network path and a live token. Front-loading the parts with no
external dependencies means a deployable app exists before any of that.

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
| **Transport** | SSE, resumable by `Last-Event-ID` | REST only — no stream of its own (§6 is cut; the app polls) |
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
| API → Hermes | private link, HTTP `:8642` | per-user `agent_api_key` (§10) | delegations + chat (existing `HermesAdapter`) |
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
  [hermes posts] ─┼─► new ─┬─────────────────► acted       (done, terminal — user only)
                  │  ▲     │                      ▲
                  │  │     ├─► snoozed ───────────┼──┐ (snooze_until passes → new)
                  │  │     │                      │  │
                  │  │     └─► delegated ──┐       │  │
                  │  │                     │       │  │
                  │  │                     ▼        │  │
                  │  └──────────────── completed ────┘  │
                  │                  (Hermes finished — triageable exactly
                  │                   like `new`, not terminal)
                  └──────────────────  (Hermes failed → back to new, error attached)
```

Six states. `new` is the inbox; `completed` is Hermes finishing, not the
alert being closed — that's still yours to do.

**Four design calls:**

1. **`delegated` resolves into `completed`, not `acted`.** Hermes finishing a
   task and you actually closing it out are different events — `completed`
   carries `resolution_note` set to the run's own output, so there's
   something to review, and it sits in the same "needs attention" view as
   `new` until you act on or dismiss it. (Earlier drafts of this doc folded
   this straight into `acted` — five states instead of seven. That undercounted:
   it meant a finished delegation looked identical to something you'd already
   dealt with, with no way to see what Hermes actually did before it vanished
   into history.)

2. **A failed Hermes run bounces to `new`** with the failure in `resolution_note`.
   A delegation can never silently vanish.

3. **Finalizing with no note preserves whatever's already there.** `act`/
   `dismiss` only overwrite `resolution_note` if the caller actually passes
   one — so reviewing a `completed` alert's output and tapping "handled"
   with nothing typed doesn't erase what Hermes wrote.

4. **Snooze needs no background worker.** Rather than a sweeper flipping
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
| Layout | `api/` + `frontend/` | **`frontend/`** as the skill says; `gaca-api/gaca_api/` stays (routers·services·db·models·schemas) | the backend layering was built deliberately; the frontend has no such history, so it follows the skill exactly |
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
- `schemas/auth.py` — `RegisterRequest`, `UserLogin`, `TokenResponse`, `UserResponse`
  (names match calendarapp/toolshed `schemas/dto/auth.py`)
- `db/users.py`, `services/auth.py`, `utils/auth.py` (hash/verify, `create_access_token`,
  `decode_token`, `get_current_user` — same file as the other repos)
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

`delegate` lands in slice 10 — everything else here is pure state, and
delegate is the one transition that actually starts a run.

**Done when:** all four dispositions work by curl and illegal transitions 409.

---

### Slice 5 — React frontend

**Goal:** sign up, log in, mint a key.

`frontend/` — Vite + React 19 + TS + Tailwind v4, dev port 41800 proxying
`/api` → 41700. Directory name, layout and launch model copied from toolshed,
which is the closest of the two repos to the build skill.

**The load-bearing principle: the frontend calls relative `/api` in every
environment.** Vite's dev proxy handles it locally, nginx handles it in
production. No `VITE_API_BASE_URL`, no environment-specific base — toolshed's
hosting-migration doc calls this out explicitly as what keeps local and prod
identical, and it's why the `/api` prefix work in slice 0 mattered.

**This is 5 teaching sub-slices, not one.** David is a backend dev new to React.

| | | Concept |
|---|---|---|
| 5a | scaffold — Vite, TS, Tailwind, one component | what the build tool does, JSX, the dev proxy |
| 5b | `api/client.ts` + `types/` | typed fetch wrapper, why relative `/api` |
| 5c | `useRouter` + `AuthContext` | hooks, state, context, URL-driven views |
| 5d | Login / Register / LandingPage | forms, controlled inputs, async submit |
| 5e | Profile — agent token + Download skill | `agentSkill.ts`, client-side file generation |

Scope is deliberately small: **signup, login, and the agent-token screen.** Alert
triage lives in the Android app. The web UI exists to get a key.

Includes phase 10's `lib/agentSkill.ts` + `agentSkill.test.ts` — the "Download
skill" button generates the markdown you hand Hermes alongside the token.

**Done when:** sign up in a browser, mint a token, download the skill file.

---

### Slice 6 — ~~SSE alert stream~~ · cut 2026-08-21

**Cut.** The original goal was a live inbox: publish to `bus.py` on every
create/transition, add `GET /api/alerts/stream` keyed by `user_id`.

Two things made it not worth building:

1. **It was never push.** It only helped if the inbox screen happened to be
   open at the exact instant Hermes posted — everything else (backgrounded,
   phone locked) still needed FCM, which stays out of scope (§9). Thin payoff
   for a whole channel: a `bus.py` topic, a stream endpoint, and
   reconnect-with-backoff on the Android client.
2. **It duplicated a stream that already exists for the thing that matters.**
   The interactive back-and-forth worth being live is watching Hermes work
   *after* you delegate — and that's `GET /api/sessions/{id}/events`
   (slice 10 below), unchanged by this cut.

**Replaced by:** the Android app does a plain `GET /api/alerts` on
app-foreground, plus pull-to-refresh (slice 12). No server-side work at all —
nothing to build, nothing to cut later if FCM shows up and makes it moot
anyway. `api-contract.md` §5 has the client-facing version of this reasoning.

---

### Slice 7 — Docker · shipped 2026-08-21

**Goal:** `docker compose up --build`.

Multi-stage `Dockerfile` (backend / frontend-build / nginx), `docker-compose.yml`,
`frontend/nginx.conf` with the SPA fallback and the `/api/` proxy.

**SQLite needs a volume** — the skill's compose assumes MongoDB on the host and
has no volume for app data. Mount the container's `/var/lib/gaca` and set
`GACA_DB_PATH=/var/lib/gaca/gaca.db`, or the database dies with the container.

**A host bind mount, not a named Docker volume — updated 2026-08-21.** Shipped
first as a named volume (`gaca-db:`); switched after realizing it fights the
app's own stated philosophy (§5: "SQLite + aiosqlite — one file, backups are a
file copy"). A named volume buries that file under
`/var/lib/docker/volumes/.../_data`, root-owned, not something you can `cp`
without `sudo` — exactly the friction the file-based design was meant to
avoid. `docker-compose.yml` now mounts `${GACA_DATA_DIR:-./data}` from the
host. **`GACA_DATA_DIR` has to be somewhere you already own** — `/var/lib/gaca`
itself is root-owned on Debian/Ubuntu/Raspberry Pi OS (can't `mkdir` there
without `sudo`, and if Docker auto-creates it, it's root-owned and the "plain
file copy" backup story breaks again). David's dev box uses `~/gaca-data`; the
Pi will need its own `GACA_DATA_DIR` set the same way — a user-owned directory,
not the FHS path literally.

**Two deviations from the skill's template, both deliberate:**

- **Two-step `uv sync`.** The skill's Dockerfile does `COPY pyproject.toml
  uv.lock ./` then `RUN uv sync --frozen --no-dev` then `COPY api/ api/` — but
  `gaca_api/` doesn't exist yet at that `uv sync`, and this project *is*
  installed into its own venv (`pyproject.toml` declares hatchling as the
  build backend), not just its dependencies. Split it: `uv sync --frozen
  --no-install-project --no-dev` first (deps only, cacheable independent of
  source changes), then `COPY gaca_api/ gaca_api/`, then a second `uv sync
  --frozen --no-dev` to install the project itself. This is uv's own
  documented pattern for exactly this case.
- **CMD is `uv run gaca-api`, not a bare `uvicorn ... --host --port`.** The
  console entry point (`gaca_api.main:run`) already reads
  `GACA_API_HOST`/`GACA_API_PORT` from settings — the same env vars that
  matter for the loopback-vs-`0.0.0.0` distinction between bare-metal dev and
  a container. Invoking uvicorn directly would mean setting the bind address
  twice, once as a CLI flag and once as the env var docker-compose.yml sets.

`frontend/nginx.conf`'s `/api/` location sets `proxy_buffering off` — the
session-events endpoint is SSE (a long-lived response), and nginx buffers by
default, which would hold the whole stream until it closed instead of
forwarding each event as it arrived.

**Done when:** the full stack runs from compose on the dev box. Verified:
`docker compose build` (both images), then `docker compose up -d` — API
reachable on `:41700`, frontend's nginx serves the SPA on `:41800` and its
`/api/` location correctly reaches the `api` container by Compose service
name. Torn down after (`docker compose down -v`) — this was a build
verification, not a service left running.

---

### Slice 8 — gaca-api provisioning · in progress, started 2026-08-21

**Goal:** the topology in §2, live. (Box is actually named `gaca-api`, not
`gaca-pi` — arm64, reachable at that hostname on the LAN. Naming carried over
from §2's diagram; not worth renaming everywhere.)

**Architecture updated from what this section originally sketched.** The
original plan was one nginx doing both edge duty and the SPA/API proxy.
Mirrored `piservices` instead (David's other box, running calendarapp the same
way) once it turned out to already have a proven, hardened two-tier pattern:

- A standalone **`gateway` stack** (nginx + cloudflared) — not part of this
  repo, lives at `~/gateway/` on the box, same as `piservices`. nginx bound to
  `127.0.0.1:8080` only, never on a public interface; Cloudflare Tunnel is the
  *only* inbound path. Pinned image digests, `read_only` root + `tmpfs`,
  `no-new-privileges`, cloudflared as non-root (`1000:1000`), capped JSON
  logging, a `/healthz` endpoint, and a catch-all `default_server` that 404s
  anything not explicitly routed.
- gaca's own stack (`compose.production.yml`, in this repo) joins an
  **external network** (`gaca-proxy`) the gateway also sits on, the same way
  `calendarapp-frontend` does on `piservices` — reachable by container name,
  nothing published to a host port at all. `api` uses `expose` only; `frontend`
  has no `ports:` section — it's `gaca-frontend`'s own nginx (SPA + `/api`
  proxy, same as local dev) that the gateway's nginx proxies to.

**Done so far:**
- Docker installed via Ubuntu's own repo (`docker.io` + `docker-compose-v2`) —
  not Docker's official third-party apt repo, since that needs a GPG-key/apt-source
  write that wasn't in the NOPASSWD sudoers scope granted for this. Functionally
  equivalent for our purposes; the pinned image digests are what actually matter
  for reproducibility, not the host engine version.
- `gaca-proxy` external Docker network created.
- `ufw default deny incoming` + `allow OpenSSH`, enabled — **verified from a
  fresh connection afterward that SSH still worked** before moving on, given
  there's no console access to recover from a lockout.
- `~/gateway/compose.yml`, `~/gateway/nginx.conf`, `~/gateway/cloudflared.yml`
  written on the box, mirroring `piservices/gateway/` exactly (same pinned
  digests — both arm64 boxes). **Hostname decided and filled in:**
  `goodandcheapagent.com` — a dedicated domain for GACA, not a shared one like
  `rabidflamingos.com`, so it's the bare apex, not a subdomain.
- `compose.production.yml` added to this repo, mirroring calendarapp's
  `compose.production.yml`: required secrets use `${VAR:?message}` (fail loudly,
  no silent throwaway default), `init: true`, `no-new-privileges`, capped
  logging, healthchecks on both services.

**Done, 2026-08-22:** tunnel created, DNS live at `goodandcheapagent.com`,
repo cloned to `~/gaca` on the box, `compose.production.yml` and the gateway
stack both up and verified end-to-end against the public URL (including the
sessions-auth fix from §10 — `POST /api/sessions` with no token correctly
401s through the real Cloudflare path, not just locally).

**GACA_AGENT_API_KEY/GACA_AGENT_BASE_URL never went in `gaca-api`'s `.env` in
the end** — §10 replaced that whole approach mid-slice with per-user agent
config. What's left here isn't an env var, it's confirming David's own
account on the live site actually has `agent_base_url`/`agent_api_key`
submitted via the profile page (the field was empty before that UI existed).

**Still open:**
1. Restricted SSH key from `gaca-api` → the Hermes box (`gaca-reader` user or
   `command=`) — not started. Nothing built so far actually uses this channel
   yet (no code path SSHes from gaca-api to Hermes), so it's not blocking —
   needed before "reading Hermes files, diagnostics" (§2's third channel)
   becomes real.
2. Confirm the agent-config submission above actually happened on the live
   account — the one thing standing between "the box is up" and "delegating
   an alert actually reaches Hermes."

**Prerequisite (unchanged):** confirm `gaca-api` and the Hermes box share a
private network, or this needs Tailscale/WireGuard instead of plain LAN.

**Done when:** Hermes posts through Cloudflare, and `gaca-api`'s ports are
closed from the LAN. Ports are closed and verified (ufw, §above); **Hermes
hasn't posted anything yet** — that's slice 9, not this one.

---

### Slice 9 — The Hermes skill · ⭐ first end-to-end — shipped 2026-08-23

**Goal:** Hermes actually posts an alert you can see.

**Shipped, and it deviated from this spec in two ways that turned out not to
matter — full writeup in [`hermes-setup.html` Step 13](../hermes-setup.html#step13).**
The skill installed as `~/.hermes/skills/productivity/david-alerts/`, not
`~/.hermes/skills/gaca/gaca-alerts/` — existing category, different name,
same durability in practice since `productivity` is already a live,
writable, non-Nix-store path. It ships a companion `scripts/gaca.py` (stdlib
only) alongside `SKILL.md`, which the spec below didn't anticipate but is a
strict improvement — no dependency to install, and it independently landed on
most of the same hardening this section calls for. **Verified live, not just
installed:** authenticates against `goodandcheapagent.com`
(`GET /api/auth/me` → 200), and a full delegate round trip — post an alert
with `suggested_actions`, delegate it, watch Hermes post a confirming
follow-up alert — has been proven twice.

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
at this stage; only the base URL changes at slice 10. **Met** — against the
real public URL, not localhost, since slice 8 already had it deployed by the
time this shipped.

---

### Slice 10 — Delegation · shipped 2026-08-21, out of order

**Goal:** hand an alert back to Hermes.

**Why it shipped before 7–9, not after.** The ordering note at the top of this
doc defers "the Hermes slices" because they need a second machine, a network
path, and a live token. That's true for slice 9 (teaching Hermes the skill) and
slice 11 (cron) — both require Hermes actually saying something. It was never
true for this slice's *code*: `delegate()` only calls
`services/runs.py:start_turn`, which is the session/chat machinery that
predates this alerting build entirely and already runs against the local dev
box (`GACA_AGENT_ENGINE=hermes`, `GACA_AGENT_BASE_URL=http://127.0.0.1:8642`).
Nothing here needed gaca-pi or a Cloudflare Tunnel to build or test. Completing
the alert API surface (create/read/triage/delegate, all of it) was worth doing
now rather than leaving one endpoint dangling until slice 8.

- `services/alerts.py`: `delegate(user_id, alert_id, instruction=, suggested_action_index=)`
  — creates a session, builds the prompt from the alert (chosen
  `suggested_action`'s prompt, then `instruction` layered on top, then a
  title+body fallback), calls `services/runs.py:start_turn`, stores
  `session_id`, sets `status=delegated`
- `services/runs.py`: `start_turn`/`read_run` grew an optional `on_settled`
  hook — awaited once with the terminal `RunCompleted`/`RunFailed` event. Kept
  generic on purpose: the invariant core still doesn't know what an Alert is,
  it just offers a callback any caller can hang logic off of
- On `run.completed` the alert settles to `completed`, with `resolution_note`
  set to the run's own output — not `acted`, since Hermes finishing a task
  isn't the same as you closing it out; on `run.failed` it bounces to `new`
  with the error in `resolution_note` — same "never silently vanish" rule as
  every other terminal outcome (§4), just triggered by the agent instead of a
  hand
- `routers/alerts.py`: `POST /api/alerts/{id}/delegate`
- A bad `suggested_action_index` (out of range for that alert) is a new
  `InvalidRequest` domain error → 422 — same shape as `NotFound`/`InvalidTransition`,
  just for "well-formed JSON, semantically wrong given this resource's state"
- Tests: `tests/services/test_alerts.py` — delegate marks `delegated` and
  returns immediately (non-blocking); a stubbed completed run settles to
  `completed` with the output recorded; a stubbed failed run bounces to `new`
  with the error recorded; terminal alerts reject delegation; a `completed`
  alert remains triageable (act/dismiss/snooze/re-delegate); prompt-building
  precedence (action vs. instruction vs. fallback); the out-of-range index
  rejection

**Done when:** delegating an alert starts a real run (or, in tests, a stubbed
one), the alert flips to `delegated` immediately, and it settles to
`completed` (with the run's output visible) or bounces to `new` once the run
ends. Met against the stub; a live check against the real local Hermes
gateway is David's to run, same working agreement as everywhere else Hermes
is involved — but the API-side mechanics are done and tested independent of
that.

---

### Slice 11 — Hermes cron · skipped, decided against 2026-08-23

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

### Slice 12 — Android app · shipped 2026-08-23

**Goal:** the triage surface.

Built against [`api-contract.md`](api-contract.md), which is written to stand
alone for exactly this. Kotlin + Compose. Deliberately last: by this point the
API is deployed, reachable over Cloudflare, and Hermes is already filling the
inbox with real alerts — so the app is built against live data rather than
fixtures.

Reads the inbox with a plain `GET /api/alerts` on app-foreground plus
pull-to-refresh — no stream to manage (§6 is cut). Out of scope for this plan:
FCM push; `severity` is the seam when we want real background notifications.

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

**Not here, on purpose: `GACA_AGENT_BASE_URL` / `GACA_AGENT_API_KEY`.** Shipped
as env vars in slices 2/10, removed 2026-08-22 — see §10. They're per-user
now (`UserDoc.agent_base_url` / `agent_api_key`, submitted via
`PUT /api/auth/agent-config`), not app config. `GACA_AGENT_ENGINE` (which
*kind* of engine — still only `hermes`) stays an env var; it's the one part
of "which agent" that's genuinely app-wide.

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
| 10 | delegate marks `delegated` and returns without waiting; `run.completed` settles to `completed` with the output recorded; `run.failed` bounces to `new` with the error recorded; terminal alerts reject delegation; a `completed` alert stays triageable; out-of-range `suggested_action_index` → 422 |

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
decide. **Not built speculatively — revisit once Hermes has been running for
real (post slice 9) and a genuine mid-run question shows up.**

**Android push — resolved 2026-08-23, no longer open.** Built ahead of slice
12 itself, since the Android app needed somewhere to register a token
against: `models/push_token.py`, `db/client.py`'s `push_tokens` table,
`services/push.py` (Firebase Admin SDK, best-effort), `POST /api/push/register`
+ `/unregister` (`routers/push.py`). Fan-out wired into `services/alerts.py`
at the two moments that matter — a new/re-fired alert, and a delegation
settling either way — not a general-purpose event stream; §6's reasoning for
cutting the SSE alert-list stream still holds; this doesn't reopen it.
`api-contract.md` §4a is the Android-facing contract. `GACA_FCM_CREDENTIALS_PATH`
(a Firebase service-account key) is optional — unset disables sending, not a
startup failure.

**Password reset.** Deferred from slice 1 — needs an SMTP decision, and single-user
doesn't need it yet.

---

## 10. Multi-tenant agent credentials — retrofit, shipped 2026-08-22

**Why this happened outside the slice plan.** Deploying to a public hostname
(slice 8) surfaced a real security gap that predated the whole alerting
build: `POST /api/sessions` and everything under it had **no auth at all** —
not "shared across accounts," genuinely unauthenticated. Registration was
also wide open. Anyone who found the URL could `curl` a session into
existence and drive David's actual Hermes box — local terminal, SSH reach
into `piservices`, a GitHub PAT — with no account and no credential of their
own. Closing that took two decisions:

1. **Require auth on every session route**, scoped to the caller — not
   optional regardless of what else changed. `SessionDoc` gained `user_id`;
   `ensure_exists` became `ensure_owned` (same 404-not-403 shape as alerts).
2. **Real per-user agent credentials, not a locked-down single account.**
   The minimal fix would have been gating registration to just David. Went
   with the bigger change instead: each user submits their own
   `agent_base_url` + `agent_api_key` via `PUT /api/auth/agent-config`
   (`GET` to check status — never echoes the key back, same rule as a
   password). There is no shared, app-wide agent target anymore.

**What that meant for the agent runtime.** `HermesAdapter` already took
`base_url`/`api_key` per instance, so the actual rework was
`agent/runtime.py`: the old single `_backend` singleton became
`backend_for(base_url, api_key)`, a small cache keyed by credentials. Raises
`AgentNotConfigured` (400) if either is missing — a normal state for a new
user, not a wiring bug. `services/runs.py` now takes the resolved `AgentBackend`
as a plain argument instead of importing the runtime itself, which if
anything made it *more* decoupled — it still knows nothing about users or
credentials, just "a backend, handed to me."

**`GACA_AGENT_BASE_URL` / `GACA_AGENT_API_KEY` are gone** from `config.py`,
`.env.example`, and both compose files. `GACA_AGENT_ENGINE` (the engine
*type* — still only `hermes`) is the one agent setting that's still app-wide.

**Frontend:** a new "Your agent" section on the profile page, above the
existing agent-token section — base URL + key, a configured/not-configured
indicator. Ordered first on purpose: minting a GACA agent token is pointless
until there's an agent to delegate to.

**Not done as part of this:** registration is still open to anyone (closing
the auth gap made that less urgent, not moot — an unlimited number of accounts
can still each configure *their own* agent and use this app for free, which
is a cost/abuse question, not a security one). Revisit if that matters before
this ever gets shared beyond one person.

---

## 11. What isn't in this build

- A live SSE alert stream (slice 6, cut 2026-08-21 — see its heading)
- Renaming the streaming `Event` concept (decided against)
- A Hermes pull/work queue (unnecessary — the API can reach Hermes)
- A Hermes plugin or MCP server (skill first; MCP is the escalation)
- Revocable DB-backed agent keys (phase-10 JWTs chosen instead)
- Alert triage in the web UI (Android owns triage)
- Password reset (FCM push shipped 2026-08-23 — see §9's "Android push")
- Gating registration — anyone can still create an account (§10) and use it
  for free with their own agent; that's a cost/abuse question left open, not
  a security one — the actual security gap (unauthenticated sessions) is closed
