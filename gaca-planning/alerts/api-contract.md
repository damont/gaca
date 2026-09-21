# GACA Alerts & Chat — HTTP contract

**Status:** Alerts (create, read, triage, delegate), Crew management, Chat
sessions, and Skills are all implemented and tested server-side ·
**Date:** 2026-08-07, updated 2026-09-20 (crew rename + chat/skills)
**Audience:** the Android app (and anything else reading the inbox or chatting)

Standalone by design — you can build the app against this without reading
[`build-plan.md`](build-plan.md).

> **The API is the source of truth, not this file.** The live schema is at
> `<base>/api/openapi.json` and a browsable UI at `<base>/api/agent`. If they
> disagree with this document, they win.

---

## 1. Base URL and auth

| Environment | Base URL |
|---|---|
| Dev, same machine | `http://localhost:41700` |
| Dev, phone on LAN | `http://<dev-box-ip>:41700` (needs `GACA_API_HOST=0.0.0.0`) |
| Production | `https://gaca.<yourdomain>` (Cloudflare Tunnel → gaca-pi) |

Every request carries one header:

```
Authorization: Bearer <jwt>
```

That's the whole auth model. No refresh tokens, no OAuth, no per-endpoint scopes.

**Two ways to get a JWT — same format, different lifetime:**

```
POST /api/auth/login                    POST /api/auth/agent-token
  { "email": …, "password": … }           { "expires_in_days": 90 }
  → 7-day session token                   → 1–365 day token
  public                                  requires an existing session token
```

The Android app should use **`/api/auth/login`** and store the session token.
`agent-token` exists for Hermes and headless scripts.

Tokens do not auto-refresh. On `401`, send the user back to the login screen.

---

## 2. Agent and crew — connect once, then add profiles

**There is no shared, app-wide agent.** Every account connects its own, in
two steps that map onto two different resources:

1. **`/agent`** — the one Hermes agent this account talks to. Connect once
   (`POST /agent/connect`, host + port + API key); this also creates your
   first crew member automatically, so there's already someone to chat with.
   Move it to a new host later with `PUT /agent`.
2. **`/crew`** — named profiles under that agent. A brand-new account starts
   with exactly the one crew member `/agent/connect` created; add more with
   `POST /crew` if the same box runs several purpose-built Hermes profiles
   (e.g. "toolshed", "trading"), each its own port + API key.

**Why two resources and not one form:** connecting is a one-time "is there
an agent at all" question with a real error mode (already connected); adding
a crew member is a repeatable "one more profile" question with a different
one (duplicate name). Collapsing them caused real confusion building the
first client against this API — see `build-plan.md` §11.

A brand-new account can create and triage alerts (act/dismiss/snooze) with
no agent connected at all. Chat (`POST /sessions`) and delegation
(`POST /alerts/{id}/delegate`) are the two things that need one; both `400`
with `CrewNotConfigured` if no crew member resolves, rather than failing
some other way.

**What's a Hermes implementation detail, not an API concept:** a crew member
happens to be a separate Hermes process on its own port under the hood
(rather than one process multiplexing several profiles) — that's why
`port`/`api_key` exist on `CrewMember` at all. Nothing about *why* is part of
this contract; treat `port` as an opaque addressing detail the API needs
back, not something to build UI copy around.

### `/agent`

| | |
|---|---|
| `GET /api/agent` | `{ "connected": bool, "host": string \| null }` |
| `POST /api/agent/connect` | body `{ "host", "port", "api_key", "name"? }` (`name` defaults `"default"`) → `201` + the full crew picture (below). `422` if already connected — this account gets one agent; use `PUT /agent` to move it, `POST /crew` to add another profile on it |
| `PUT /api/agent` | body `{ "host" }` — move the connected agent to a new host. Leaves the crew list untouched |

### Having gaca-api create a crew member for you — no separate setup call

**There is no dedicated "enable this" resource or endpoint.** Earlier drafts
had one (`/agent/provisioning`) and it was the wrong shape — it made a
one-time implementation detail (does gaca-api have a way to reach the box
directly) into a whole separate thing the caller had to set up, check the
status of, and test before it could ever call create. Corrected the same
day: it's just one more optional field on the calls that actually need it.

`POST /crew` and `POST /crew/discover` (below) both take an optional
`access` field:

```jsonc
"access": { "host": "david-claw.lan", "port": 22, "username": "david", "private_key": "…" }
```

Give it once, on whichever of those two calls you make first — gaca-api
stores it and remembers it for every call after, so you never have to send
it again. Omit it once gaca-api already has it. **How gaca-api actually
uses this (SSH, today) is not part of this contract** — treat it as an
opaque access credential for the box, the same way `CrewMember.port` is an
opaque addressing detail (§2), not something to build UI copy or a
dedicated settings screen around. `GET`/`POST`/etc. on `/crew` all return
`can_create` (see the CrewOut shape below) so a client can tell, from data
it's already fetching, whether its "add crew member" form needs to ask for
`access` or can just ask for a name.

A failed provisioning call (bad credential, unreachable box, name already
exists as a Hermes profile) is `502`, with the real underlying error in the
response body's `detail` — there's no separate "test" call; attempting the
real action *is* the test.

### The CrewMember object

```jsonc
{
  "name": "toolshed",
  "port": 8643,
  "color": "#6B8F5A",   // hex, always present — see "Color" below
  "is_default": false
}
```

Never carries the API key — same rule as a password.

### Color

Every crew member has a persistent hex color, auto-assigned from a fixed
6-hue palette when it's created (first swatch not already in use), unless
the caller supplies one. **Use it everywhere that crew member shows up** —
avatar, chat header accent, that crew member's own message bubbles, filter
chips, a settings-row border — so which profile you're talking to is obvious
at a glance without reading its name every time. It's a plain hex string the
client renders as-is; the server is the one source of truth for it; nothing
about the palette itself is part of this contract, just the field.

### The CrewOut shape — what every `/agent` and `/crew` call returns

```jsonc
{
  "host": "http://david-claw",
  "crew": [ { /* CrewMember */ }, … ],
  "can_create": true   // does gaca-api already have what it needs to
                        // create a new one for you (§ above) — lets an
                        // "add crew member" form decide whether it needs
                        // to ask for `access` or just a name
}
```

Every mutating `/agent` or `/crew` endpoint returns this same shape, so the
client can just replace its whole local state after any change rather than
patching it.

### `/crew` endpoints

| | |
|---|---|
| `GET /api/crew` | the CrewOut shape above |
| `POST /api/crew` | body `{ "name", "port"?, "api_key"?, "color"?, "access"? }` → `201` + CrewOut. Give `port`+`api_key` together to register one you set up by hand (`422` if only one of the two is given); omit both to have gaca-api create it — needs `access` (see above), given on this call or a prior one, else `400`. Adds *another* profile — the first one is created by `/agent/connect`, not here |
| `POST /api/crew/discover` | body `{ "access"? }`, or no body at all → CrewOut. Finds crew members already running on the box but never registered through gaca-api, and adds them. Read-only against the box; never overwrites one already known by that name. Same `access` rule as create — `400` if gaca-api has never been given it |
| `PUT /api/crew/{name}` | body `{ "port", "api_key", "color"? }` — port+key required together (no partial update); color alone can't change without them |
| `DELETE /api/crew/{name}` | `422` if it's the current default and others remain — set a different default first |
| `POST /api/crew/{name}/default` | make this the fallback for a chat/delegation that doesn't name one |
| `GET /api/crew/{name}/skills` | see §5 |

---

## 3. The Alert object

```jsonc
{
  "id": "01JX8K2M4P7QRSTVWXYZ",
  "user_id": "01JX8K2M4P7QRSTVWXY0",
  "created_at": "2026-08-07T14:22:05.123456Z",

  "source": "hermes-prod",          // who posted it. ADVISORY ONLY — supplied in
                                    // the request body, not authenticated. Do not
                                    // use it for trust decisions; `user_id` is the
                                    // real ownership boundary
  "crew": "toolshed",               // nullable. Which crew member posted this —
                                    // distinct from `source` (the channel).
                                    // Same trust level: advisory, unauthenticated
  "kind": "disk.low",               // free-form dotted string — do NOT switch on
                                    // an exhaustive list; new kinds appear without
                                    // an API release
  "severity": "warn",               // "info" | "warn" | "urgent"

  "title": "Disk is 91% full on hermes-box",
  "body": "…longer markdown…",      // nullable
  "data": {                         // arbitrary — whatever Hermes saw
    "mount": "/", "used_pct": 91, "free_gb": 12.4
  },

  "dedupe_key": "disk.low:/",       // nullable
  "suggested_actions": [
    { "label": "Clean up old logs", "prompt": "Free space on hermes-box by …" },
    { "label": "Show me what's big", "prompt": "List the 20 largest files under …" }
  ],

  "status": "new",                  // stored status — see §4
  "effective_status": "new",        // what to DISPLAY — see §4
  "status_changed_at": "2026-08-07T14:22:05.123456Z",
  "snooze_until": null,             // set when snoozed
  "session_id": null,               // set when delegated — the chat session running it (§6)
  "resolution_note": null,          // how it ended, or why it failed
  "parent_alert_id": null           // set on gaca.clarify children (may not ship initially)
}
```

**Three things to get right in the client:**

1. **`kind` is open-ended.** Render unknown kinds generically; never assume a
   closed set. Hermes invents kinds without an API deploy.
2. **`severity` drives presentation only.** Three values, stable. Safe to switch on.
3. **`suggested_actions` may be empty.** When present, render as buttons —
   tapping one sends its `prompt` to `/delegate`. This is the primary interaction,
   not a nice-to-have.

---

## 4. Status — read `effective_status`, send transitions against `status`

Six stored states:

```
new · snoozed · delegated · completed · acted · dismissed
```

**`completed` is Hermes finishing a delegated run — not the same as the alert
being closed out.** It carries `resolution_note` set to the run's own output,
and it's triageable exactly like `new` (act/dismiss/snooze/re-delegate all
work on it). Only `act`/`dismiss` — a real user action — reach a terminal
state from here. Treat `completed` as belonging in the same "needs attention"
list as `new`, not in history.

**Snooze is computed, not swept.** There's no background worker flipping expired
snoozes back. The API derives:

```
effective_status = "new"  if status == "snoozed" and snooze_until <= now
                 else status
```

**The app should display and filter on `effective_status`.** `status` is the raw
stored value and is included for debugging.

Legal transitions:

| From | To | Via |
|---|---|---|
| `new` / `completed` | `delegated` | `POST /alerts/{id}/delegate` |
| `new` / `completed` | `acted` | `POST /alerts/{id}/act` |
| `new` / `completed` | `dismissed` | `POST /alerts/{id}/dismiss` |
| `new` / `completed` | `snoozed` | `POST /alerts/{id}/snooze` |
| `snoozed` | any of the five | same (an expired snooze reads as `new`) |
| `delegated` | `completed` | automatic — Hermes finished; `resolution_note` gets the run's output |
| `delegated` | `new` | automatic — Hermes failed; see `resolution_note` |

`acted` and `dismissed` are terminal; `completed` is not. **`delegated` is
also not manually triageable** — it's settled by the run working on it.
Anything else returns **409**.

**Finalizing with no note keeps whatever `resolution_note` was already
there** — `act`/`dismiss` only overwrite it if you actually pass one. So
reviewing a `completed` alert's output and tapping "handled" with nothing
typed doesn't erase what Hermes wrote.

A `409` means the alert moved underneath you: a run settled it, or you triaged
it on another device. Refetch and re-render rather than retrying.

---

## 5. Alert endpoints

### `GET /api/alerts`

| Query param | Type | Default | Notes |
|---|---|---|---|
| `status` | string | *(all)* | filters on **effective** status; repeatable |
| `severity` | string | *(all)* | repeatable |
| `crew` | string | *(all)* | repeatable |
| `since` | ISO-8601 | — | `created_at >` |
| `limit` | int | `50` | max 200 |

```jsonc
// GET /api/alerts?status=new&status=snoozed&limit=50
{
  "alerts": [ { /* Alert */ }, … ]
}
```

`status`, `severity`, and `crew` are repeatable — pass them more than once to
widen the filter. **Cursor pagination is not implemented yet**; `limit` alone
is the whole story until an inbox gets big enough to need paging.

Newest first. Always scoped to the authenticated user — there is no cross-user read.

### `GET /api/alerts/{id}`

One Alert, or `404`.

### `POST /api/alerts/{id}/delegate`

Hand it to a crew member. Opens a chat session (bidirectionally linked —
see §6) and starts a run.

```jsonc
// request — all fields optional
{
  "instruction": "Free space but don't touch anything under /var/lib/gaca",
  "suggested_action_index": 0,   // convenience: use that action's prompt
  "crew": "toolshed"              // which configured crew member handles it — see §7
}
```

Sets `status=delegated` and populates `session_id`. Returns the updated Alert
immediately — the run itself keeps going in the background.

**How the two combine, if both are given:** `suggested_action_index` seeds the
prompt with that action's own text; `instruction` is appended after it as extra
guidance. With `instruction` alone, that's the whole prompt. With neither,
the prompt falls back to the alert's own `title` + `body`.

**Settlement is automatic, not a client action.** When the run ends, the API
watches its outcome and moves the alert on its own: `run.completed` →
`completed`, with `resolution_note` set to the run's own output — not
`acted`, since Hermes finishing isn't the same as you closing it out;
`run.failed` → back to `new`, with the error in `resolution_note` (same "never
silently vanish" rule §4 applies to every other terminal outcome). Poll
`GET /api/alerts/{id}` — or the session's own stream, §6 — to see it land.

### `POST /api/alerts/{id}/act` · `/dismiss`

```jsonc
{ "note": "handled by hand" }   // optional
```

Terminal. Returns the updated Alert.

### `POST /api/alerts/{id}/snooze`

```jsonc
{ "until": "2026-08-08T09:00:00Z" }   // required, must be in the future
```

### `POST /api/alerts` — Hermes writes here

Documented so the app can render what Hermes sends; the app itself has no reason
to call it.

```jsonc
{
  "kind": "disk.low",
  "severity": "warn",
  "title": "Disk is 91% full on hermes-box",
  "body": "…",
  "data": { "mount": "/", "used_pct": 91 },
  "dedupe_key": "disk.low:/",
  "suggested_actions": [ { "label": "…", "prompt": "…" } ]
}
```

`201` on create, `200` on dedupe-update — so a poster can tell whether it
actually told the user something new. **Re-posting an existing
`(user_id, dedupe_key)` updates the row and resets `status` to `new`**, clearing
`snooze_until`, `session_id` and `resolution_note`, so a re-fired condition
resurfaces even if it was dismissed. `id` and `created_at` are preserved — it's
the same alert, updated.

`user_id` comes from the token and cannot be set by the caller. `source` is
ordinary body data and is **not** authenticated — our tokens carry only a
subject and an expiry, so there is no key label to read.

---

## 6. Chat — sessions and messages

A session is a standing conversation with exactly one crew member, fixed for
its whole lifetime (no mid-thread swap — start a new session to talk to
someone else). It can start two ways: directly from the Chat tab, or as the
side effect of delegating an alert. Either way it's the same object and the
same endpoints from here on.

### The Session object

```jsonc
{
  "id": "01JX9…",
  "created_at": "2026-09-20T14:00:00Z",
  "title": "Disk usage on gaca-pi",   // nullable
  "status": "idle",                   // idle | active
  "crew": "toolshed",                 // which crew member this session talks to
  "alert_id": "01JX8K2M4P7QRSTVWXYZ", // set only if this session was opened by
                                       // a delegation — null for a session
                                       // started directly from the Chat tab
  "messages": [ { /* Message, GET only */ } ]
}
```

**Bidirectional alert↔chat link:** an alert delegated via `/delegate` carries
the forward pointer (`Alert.session_id`); the session it opens carries the
back pointer (`Session.alert_id`). A chat screen with a non-null `alert_id`
can render a "from alert" pill and `GET /api/alerts/{alert_id}` to show it;
an alert screen with a non-null `session_id` can open that session directly.
A session started from the Chat tab has `alert_id: null` — nothing to link
back to.

### `GET /api/sessions`

```jsonc
{ "sessions": [ { /* Session, no messages */ }, … ] }
```

**Most recently active first** — the newest message in a session, not when
the session itself was created, so a months-old session that just got a
reply jumps back to the top. A session with no messages yet sorts by its
own creation time instead, since there's no message to go by. The Chat
tab's list. Always scoped to the authenticated user. No filtering or
pagination yet — revisit if a single user's chat list ever gets big enough
to need it, same as the alert list (§5).

### `POST /api/sessions`

```jsonc
// request — both optional
{ "title": "…", "crew": "toolshed" }
```

`crew` names which of the account's crew members this chat talks to; omit it
to use the account default. Unlike `/delegate`, there's no alert to
soft-match against — just the explicit name or the default. `201` + the
Session (no messages yet).

### `GET /api/sessions/{id}`

The Session plus every message in it, oldest first.

### `POST /api/sessions/{id}/messages`

```jsonc
{ "content": "What's using all the disk space?" }
```

`202` immediately (`{ "turn_id": "…" }`) — this does not wait for a reply.
The user message is persisted right away; the reply streams in on the events
endpoint below and lands in the transcript when the run completes.

### `GET /api/sessions/{id}/events`

Server-Sent Events. Reconnect with `Last-Event-ID` to replay anything missed
— events are durable and sequenced, not fire-and-forget. Event types:
`assistant.delta`, `tool.started`, `run.completed`, `run.failed`. This is the
one live stream in the API (see §8 for why the alert list itself is plain
polling instead).

---

## 7. Which crew member handles a delegation

`POST /{id}/delegate` takes an optional `crew` field naming one of the
account's configured crew members. Resolution order:
1. `crew` in the request body, if given — an explicit, deliberate choice, so
   naming one that isn't configured is a `422`, not a silent fallback.
2. Otherwise the alert's own `crew` field (who raised it), if it matches a
   configured name.
3. Otherwise the account's default crew member.

If none of the three resolves to a real crew member, it's a `400` with
`CrewNotConfigured` (§9).

A session started directly (`POST /sessions`) has no alert to fall through
via step 2 — just `crew` in the body, else the default.

---

## 8. Skills — what a crew member can actually do

Read-only insight, not control: skills load however Hermes loads them for
that crew member; the app just shows what's there. **Never cached by
gaca-api** — every call is live against that crew member's own Hermes
gateway, so it can never show a skill Hermes no longer has installed.

### `GET /api/crew/{name}/skills`

```jsonc
[
  { "name": "email", "description": "Reads and drafts Gmail messages.", "category": "productivity" },
  { "name": "smart-home", "description": "Controls Home Assistant devices.", "category": "smart-home" }
]
```

Sorted by category then name. `category` may be `null` for an uncategorized
skill. `404` if `name` isn't one of this account's configured crew members.

---

## 8a. Push registration — FCM

Both routes require the normal `Authorization: Bearer` header, same as
everything else.

### `POST /api/push/register`

```jsonc
{ "token": "…", "platform": "android" }   // platform optional, defaults "android"
```

`204` on success. Call this on every app launch and whenever
`FirebaseMessagingService.onNewToken()` fires — tokens rotate, so this is
"keep me current," not a one-time call. Idempotent: registering the same
token again is a no-op. If the same token shows up under a different account
than before, it moves to the new one — that's the same device signing into a
different account, not two people sharing a token.

### `POST /api/push/unregister`

```jsonc
{ "token": "…" }
```

`204` on success (also `204` if the token wasn't found, or belonged to
someone else — this never leaks whether a token exists). Call on sign-out so
a shared or reset device doesn't keep alerting for the account that just
signed out.

### What triggers a push

Fan-out lives server-side (`services/push.py`), not something the app drives:
a new alert (`POST /api/alerts`, including a dedupe re-fire that resets
`status` to `new`) and a delegation settling (§5's `/delegate`, either
outcome — Hermes finishing or the run failing) each send one push per
registered device. Best-effort throughout: a missing/invalid token is dropped
server-side, and nothing about alert creation or delegation ever fails or
slows down because a push couldn't be sent. The app's own polling (§9 below)
is still the source of truth; push is a "tell me sooner" layered on top, not
a replacement.

**A standalone chat session (not opened via delegation) does not push on
completion yet** — that's a known gap, not a documented behavior; don't build
against it either way.

Every message carries `data.alert_id` and `data.severity` (not just a title/
body) so the app can deep-link straight to the alert and pick a notification
channel. **Channel selection happens twice, and the two must agree**: the
server sets `android.notification.channel_id` on the FCM message itself
(matters when the app is backgrounded — the OS displays the notification
directly without running any app code), and the app picks the same channel
by hand in `onMessageReceived` (matters when the app is foregrounded — FCM
never auto-displays in that case). Today that's a two-value map, `severity ==
"urgent"` → the urgent channel, everything else → standard — literal channel
id strings that have to match the Android app's `strings.xml` exactly, since
nothing enforces that across the two repos.

---

## 9. Reading the inbox — plain polling, no stream

There is no `/api/alerts/stream`. An earlier draft of this doc specced one
(SSE, keyed by `user_id`) for a "live while the app is open" inbox; cut on
2026-08-21 because the value was thin and the cost wasn't. Two reasons:

1. **It wasn't push.** It only helped if the inbox screen happened to be open
   at the exact instant Hermes posted something — everything else (the app
   backgrounded, the phone locked) needed FCM regardless. That's a narrow
   window to build a whole channel for.
2. **It duplicated a real stream with a fake one.** The interactive
   back-and-forth that actually benefits from being live — watching a crew
   member work — already has one: `GET /api/sessions/{id}/events` (§6). The
   alert list itself doesn't need the same treatment; you look at it, act on
   it, and move on.

**So: `GET /api/alerts` on app-foreground, plus pull-to-refresh, is still the
whole story for the alert *list*.** Simpler client, no reconnect-with-backoff
logic, no `bus.py` channel to maintain server-side. **FCM itself did land**
(§8a) — "notify me the instant this happens" is real now — but it answers a
different question than this section does: it's a push *notification*
telling you something changed, not a live *stream* of the inbox screen. The
app still fetches the alert list the same way it always did; FCM just gives
it a reason to do that sooner than the next foreground/pull-to-refresh.

---

## 10. Status codes

| Code | Meaning | What the app should do |
|---|---|---|
| `200` / `201` / `202` | Success | — |
| `400` | `CrewNotConfigured` — this user has no usable crew member: no host, no crew, or no default | Tell them to set one up (Settings → crew). Don't retry |
| `422` | Body doesn't match the schema — including `snooze until` in the past, or naming a `crew`/`suggested_action_index` that doesn't exist | Fix and don't retry |
| `401` | Token missing, malformed, or expired | Send to login. **Do not retry** |
| `403` | Authenticated but forbidden | Show the body. Don't retry |
| `404` | Doesn't exist or isn't yours | Drop it from the list; the id is stale |
| `409` | Illegal state transition | Refetch the resource — someone else moved it |
| `502` | A provisioning call (`POST /crew` creating one, `POST /crew/discover`) reached the user's own box on their behalf and that failed | Show the body's `detail` (the real underlying error) — don't retry blindly, the box may need attention |
| `5xx` | Server error | Backoff with jitter |

`409` is the one worth handling well: it means the resource moved underneath
you — a run settled a delegation, or you triaged it on another device.
Refetch and re-render rather than showing an error.

---

## 11. Division of responsibility

| | Owns |
|---|---|
| **Hermes** | Noticing things. Writing well-formed alerts — good `dedupe_key`, honest `severity`, useful `suggested_actions`. Doing delegated and chatted-with work. Its own skills |
| **gaca-api** | System of record for users, crew config, alerts, sessions and messages. Auth. State machine and its guards. Effective-status computation. Fan-out. Never caches a crew member's skills or models — always live |
| **Android** | Triage and chat UX. Rendering unknown `kind`s gracefully. Making which crew member you're talking to obvious (color, name) everywhere it's relevant. Local read/unread. Reconnect and refetch |
| **frontend** | Signup, login, minting the agent token. **Not** triage |

**The app does not compute state.** Don't locally derive whether a snooze has
expired or whether a transition is legal — send the request and honour the
response. The server owns the rules; a client that duplicates them will drift.
