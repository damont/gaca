# GACA Alerts — HTTP contract

**Status:** Design, not yet implemented · **Date:** 2026-08-07
**Audience:** the Android app (and anything else reading the inbox)

Standalone by design — you can build the app against this without reading
[`build-plan.md`](build-plan.md). Endpoints land across slices 1–8 of that plan.

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
| `since` | ISO-8601 | — | `created_at >` |
| `limit` | int | `50` | max 200 |

```jsonc
// GET /api/alerts?status=new&status=snoozed&limit=50
{
  "alerts": [ { /* Alert */ }, … ]
}
```

`status` and `severity` are repeatable — pass them more than once to widen the
filter. **Cursor pagination is not implemented yet**; `limit` alone is the whole
story until an inbox gets big enough to need paging.

Newest first. Always scoped to the authenticated user — there is no cross-user read.

### `GET /api/alerts/{id}`

One Alert, or `404`.

### `POST /api/alerts/{id}/delegate`

Hand it to Hermes.

```jsonc
// request — both fields optional
{
  "instruction": "Free space but don't touch anything under /var/lib/gaca",
  "suggested_action_index": 0    // convenience: use that action's prompt
}
```

Sets `status=delegated` and populates `session_id`. Returns the updated Alert.

> `session_id` links to an existing chat session, so the app can stream the run's
> progress on `GET /api/sessions/{session_id}/events` — the pre-existing SSE
> endpoint carrying `assistant.delta` / `tool.started` / `run.completed`.
> **That is a different stream from `/api/alerts/stream`** — see §1 of the build
> plan for why the two "event" concepts are distinct.

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

## 5. `GET /api/alerts/stream` — live inbox

Server-Sent Events, one event per create or transition. Keeps the inbox live
while the app is foregrounded.

```
event: alert.created
data: { …Alert… }

event: alert.updated
data: { …Alert… }
```

- Standard SSE — `text/event-stream`, `Authorization` header as usual.
- Channel is keyed by **`user_id`**.
- **No `Last-Event-ID` replay on this stream.** Unlike the run-event stream,
  alerts aren't sequenced. On reconnect, re-fetch `GET /api/alerts` and treat the
  stream as live-only.
- Expect idle periods. Handle keep-alive comments and reconnect with backoff.

**This is not push.** It only works while the app is open. Real background
notifications need FCM, which is out of scope for this build; `severity` is the
seam for it later.

---

## 6. Status codes

| Code | Meaning | What the app should do |
|---|---|---|
| `200` / `201` | Success | — |
| `422` | Body doesn't match the schema — including `snooze until` in the past | Fix and don't retry |
| `401` | Token missing, malformed, or expired | Send to login. **Do not retry** |
| `403` | Authenticated but forbidden | Show the body. Don't retry |
| `404` | Doesn't exist or isn't yours | Drop it from the list; the id is stale |
| `409` | Illegal state transition | Refetch the alert — someone else moved it |
| `5xx` | Server error | Backoff with jitter |

`409` is the one worth handling well: it means the alert moved underneath you —
Hermes settled a delegation, or you triaged it on another device. Refetch and
re-render rather than showing an error.

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
