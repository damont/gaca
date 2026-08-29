# GACA Alerts — HTTP contract

**Status:** Create, read, triage (act/dismiss/snooze) and delegate are
implemented and tested server-side · **Date:** 2026-08-07, updated 2026-08-21
**Audience:** the Android app (and anything else reading the inbox)

Standalone by design — you can build the app against this without reading
[`build-plan.md`](build-plan.md). Endpoints land across slices 1–4 and 10 of
that plan.

> **The API is the source of truth, not this file.** Once slice 2 ships, the
> live schema is at `<base>/api/openapi.json` and a browsable UI at
> `<base>/api/agent`. If they disagree with this document, they win.

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

## 2. The Alert object

```jsonc
{
  "id": "01JX8K2M4P7QRSTVWXYZ",
  "user_id": "01JX8K2M4P7QRSTVWXY0",
  "created_at": "2026-08-07T14:22:05.123456Z",

  "source": "hermes-prod",          // who posted it. ADVISORY ONLY — supplied in
                                    // the request body, not authenticated. Do not
                                    // use it for trust decisions; `user_id` is the
                                    // real ownership boundary
  "agent": "david-alerts",          // nullable. Which Hermes agent profile posted
                                    // this — distinct from `source` (the channel).
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

  "status": "new",                  // stored status — see §3
  "effective_status": "new",        // what to DISPLAY — see §3
  "status_changed_at": "2026-08-07T14:22:05.123456Z",
  "snooze_until": null,             // set when snoozed
  "session_id": null,               // set when delegated — the chat session running it
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

## 3. Status — read `effective_status`, send transitions against `status`

Five stored states:

```
new · snoozed · delegated · acted · dismissed
```

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
| `new` | `delegated` | `POST /alerts/{id}/delegate` |
| `new` | `acted` | `POST /alerts/{id}/act` |
| `new` | `dismissed` | `POST /alerts/{id}/dismiss` |
| `new` | `snoozed` | `POST /alerts/{id}/snooze` |
| `snoozed` | any of the four | same (an expired snooze reads as `new`) |
| `delegated` | `acted` | automatic — Hermes finished |
| `delegated` | `new` | automatic — Hermes failed; see `resolution_note` |

`acted` and `dismissed` are terminal. **`delegated` is also not manually
triageable** — it's settled by the run working on it. Anything else returns
**409**.

A `409` means the alert moved underneath you: a run settled it, or you triaged
it on another device. Refetch and re-render rather than retrying.

---

## 4. Endpoints

### `GET /api/alerts`

| Query param | Type | Default | Notes |
|---|---|---|---|
| `status` | string | *(all)* | filters on **effective** status; repeatable |
| `severity` | string | *(all)* | repeatable |
| `agent` | string | *(all)* | repeatable |
| `since` | ISO-8601 | — | `created_at >` |
| `limit` | int | `50` | max 200 |

```jsonc
// GET /api/alerts?status=new&status=snoozed&limit=50
{
  "alerts": [ { /* Alert */ }, … ]
}
```

`status`, `severity`, and `agent` are repeatable — pass them more than once to
widen the filter. **Cursor pagination is not implemented yet**; `limit` alone
is the whole story until an inbox gets big enough to need paging.

Newest first. Always scoped to the authenticated user — there is no cross-user read.

### `GET /api/alerts/{id}`

One Alert, or `404`.

### `POST /api/alerts/{id}/delegate`

Hand it to Hermes.

```jsonc
// request — all fields optional
{
  "instruction": "Free space but don't touch anything under /var/lib/gaca",
  "suggested_action_index": 0,   // convenience: use that action's prompt
  "agent": "toolshed"             // which configured agent handles it — see §6
}
```

Sets `status=delegated` and populates `session_id`. Returns the updated Alert
immediately — the run itself keeps going in the background.

**How the two combine, if both are given:** `suggested_action_index` seeds the
prompt with that action's own text; `instruction` is appended after it as extra
guidance. With `instruction` alone, that's the whole prompt. With neither,
the prompt falls back to the alert's own `title` + `body`.

**Settlement is automatic, not a client action.** When the run ends, the API
watches its outcome and moves the alert on its own: `run.completed` → `acted`;
`run.failed` → back to `new`, with the error in `resolution_note` (same "never
silently vanish" rule §4 applies to every other terminal outcome). Poll
`GET /api/alerts/{id}` — or the run's own stream, next paragraph — to see it
land.

> `session_id` links to an existing chat session, so the app can stream the
> run's progress on `GET /api/sessions/{session_id}/events` — the pre-existing
> SSE endpoint carrying `assistant.delta` / `tool.started` / `run.completed`.
> That's the only live stream in this API — see §1 of the build plan for why
> it's a different concept from an Alert, and §5 below for why the *alert list*
> itself is plain polling, not a stream.

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

## 4a. Push registration — FCM

Added 2026-08-23, no longer out of scope (§5 below explains why polling was
the whole story before this). Both routes require the normal `Authorization:
Bearer` header, same as everything else.

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
`status` to `new`) and a delegation settling (§4's `/delegate`, either
outcome — Hermes finishing or the run failing) each send one push per
registered device. Best-effort throughout: a missing/invalid token is dropped
server-side, and nothing about alert creation or delegation ever fails or
slows down because a push couldn't be sent. The app's own polling (§5) is
still the source of truth; push is a "tell me sooner" layered on top, not a
replacement.

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

## 5. Reading the inbox — plain polling, no stream

There is no `/api/alerts/stream`. An earlier draft of this doc specced one
(SSE, keyed by `user_id`) for a "live while the app is open" inbox; cut on
2026-08-21 because the value was thin and the cost wasn't. Two reasons:

1. **It wasn't push.** It only helped if the inbox screen happened to be open
   at the exact instant Hermes posted something — everything else (the app
   backgrounded, the phone locked) needed FCM regardless, which at the time
   (2026-08-21) was out of scope. That's a narrow window to build a whole
   channel for.
2. **It duplicated a real stream with a fake one.** The interactive
   back-and-forth that actually benefits from being live — watching Hermes
   work once you've delegated — already has one: `GET
   /api/sessions/{session_id}/events` (§3, `POST /{id}/delegate`). The alert
   list itself doesn't need the same treatment; you look at it, act on it, and
   move on.

**So: `GET /api/alerts` on app-foreground, plus pull-to-refresh, is still the
whole story for the alert *list*.** Simpler client, no reconnect-with-backoff
logic, no `bus.py` channel to maintain server-side. **FCM itself did land**
(§4a, 2026-08-23) — "notify me the instant this happens" is real now — but it
answers a different question than this section does: it's a push *notification*
telling you something changed, not a live *stream* of the inbox screen. The
app still fetches the alert list the same way it always did; FCM just gives it
a reason to do that sooner than the next foreground/pull-to-refresh.

---

## 6. Status codes

| Code | Meaning | What the app should do |
|---|---|---|
| `200` / `201` | Success | — |
| `400` | On `/{id}/delegate`: this user has no usable agent — no host, no agents, or no default | Tell them to set it up in the web app's profile page. Don't retry |
| `422` | Body doesn't match the schema — including `snooze until` in the past | Fix and don't retry |
| `401` | Token missing, malformed, or expired | Send to login. **Do not retry** |
| `403` | Authenticated but forbidden | Show the body. Don't retry |
| `404` | Doesn't exist or isn't yours | Drop it from the list; the id is stale |
| `409` | Illegal state transition | Refetch the alert — someone else moved it |
| `5xx` | Server error | Backoff with jitter |

`409` is the one worth handling well: it means the alert moved underneath you —
Hermes settled a delegation, or you triaged it on another device. Refetch and
re-render rather than showing an error.

**There is no shared, app-wide agent** — every account brings its own via the
web app's profile page: one shared host (`PUT /api/auth/agent-host`) plus any
number of named agents under it (`POST /api/auth/agents`, each with its own
port and key; `GET /api/auth/agents` lists them, never the keys). A
brand-new account can create and triage alerts (act/dismiss/snooze) with
nothing configured; `/delegate` is the one endpoint that needs an agent, and
400s clearly if none resolves rather than failing some other way.

**Which agent handles a delegation** — `POST /{id}/delegate` takes an optional
`agent` field naming one of the account's configured agents. Resolution order:
1. `agent` in the request body, if given — an explicit, deliberate choice, so
   naming one that isn't configured is a `422`, not a silent fallback.
2. Otherwise the alert's own `agent` field (who raised it), if it matches a
   configured agent name.
3. Otherwise the account's default agent.

If none of the three resolves to a real agent, it's the `400` above.

---

## 7. Division of responsibility

| | Owns |
|---|---|
| **Hermes** | Noticing things. Writing well-formed alerts — good `dedupe_key`, honest `severity`, useful `suggested_actions`. Doing delegated work |
| **gaca-api** | System of record. Auth. State machine and its guards. Effective-status computation. Fan-out |
| **Android** | Triage UX. Rendering unknown `kind`s gracefully. Local read/unread. Reconnect and refetch |
| **frontend** | Signup, login, minting the agent token. **Not** triage |

**The app does not compute state.** Don't locally derive whether a snooze has
expired or whether a transition is legal — send the request and honour the
response. The server owns the rules; a client that duplicates them will drift.
