# GACA Alerts & Chat — HTTP contract

**Status:** Alerts (create, read, triage, delegate), Crew management, Chat
sessions, and Skills are all implemented and tested server-side ·
**Date:** 2026-08-07, updated 2026-09-20 (crew rename + chat/skills),
2026-10-10 (iOS: APNs push, iOS Google sign-in, server-derived message and run fields;
one-call delegation, `data_rows`, `artifact`, `last_activity_at`)
**Audience:** the Android and iPhone apps (and anything else reading the inbox or chatting)

Standalone by design — you can build the app against this without reading
[`build-plan.md`](build-plan.md).

> **The API is the source of truth, not this file.** The live schema is at
> `<base>/api/openapi.json` and a browsable UI at `<base>/api/docs`. If they
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

That's the whole auth model. No refresh tokens, no OAuth access tokens, no per-endpoint scopes.

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

**Google sign-in** — `POST /api/auth/google` with `{ "id_token": "…" }` → the
same 7-day session token. The API verifies the ID token's signature and that
its audience (`aud`) is one of the server's configured OAuth client ids:
`GACA_GOOGLE_CLIENT_ID` (web/Android) or `GACA_GOOGLE_IOS_CLIENT_ID` (the iOS
OAuth client — Google Sign-In on iPhone mints tokens for that id, not the web
one). Either being set enables Google sign-in; with neither it's `500`
"Google login not configured". Both ids are public, not secrets. A bad,
expired or wrong-audience token is `401`.

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
                                    // Same trust level: advisory, unauthenticated.
                                    // When the alert is taken to a chat, THIS is whose
                                    // chat opens (then the account default). `agent` is
                                    // accepted on POST as an alias for `crew`
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
  "links": [                        // pages that go with this alert (see below)
    { "url": "https://calendar.example.com/#week-card-2026-10-05", "label": "Open family calendar" }
  ],
  "artifact_id": null,              // a page published for this alert (artifacts/api-contract.md)
  "artifact": null,                 // that page resolved for its chip, or null — see below
  "data_rows": [                    // `data` ready to show as label/value rows — see below
    { "key": "mount", "value": "/" }, { "key": "used_pct", "value": "91" }, { "key": "free_gb", "value": "12.4" }
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

**`data_rows`** is `data`'s top-level entries in order, each value already rendered as text: a
string as-is; a number, boolean or null as its JSON text (`91`, `false`, `null`); an object as
`k: v, k2: v2` and an array as items joined with `, `, both recursively. Show these rather than
walking `data` yourself — `data` is arbitrary and stays there for anything that wants the raw JSON.

**`artifact`** is `artifact_id` resolved: `{ "id", "title", "description", "crew", "missing" }`, the
same chip shape as a chat message's `artifacts` (§6). `missing: true` (title `"Page"`, the rest null)
when the page has since been deleted. `null` when `artifact_id` is. Both list and detail carry it.

**`links`** are web pages to open in order to act on the alert — the calendar week an event is in, the
email a suggestion came from, the dashboard that raised it. Up to 10; each is `{url, label?}`.
`url` must be `http://` or `https://` (anything else is a `422`; the app opens these on tap). **Responses
always carry a `label`** — if the poster gave none it's the site's name (`mail.google.com`). Put the link
here rather than only in `body`: the app shows links as buttons under the alert text, and when an alert
is taken to a chat the links are included in the text the agent gets. Like `suggested_actions`, links are
part of the alert's content: a re-post with the same `dedupe_key` **replaces** them, so restate any you
still want.

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

### `POST /api/alerts/delegations` — "have a crew member do this"

For work the user asks for directly (the app's "New delegation" screen), with no alert to start
from: one call raises the alert and delegates it.

```jsonc
// request
{ "instruction": "Renew the domain before Friday\nand tell me the new expiry",  // required, not blank, ≤ 2000
  "crew": "toolshed",   // optional — who handles it; omit for the account default
  "source": "app" }     // optional, default "app"

// 201 — the Alert, already "delegated", with session_id set
```

The alert is `kind: "manual.delegation"`, `title` = the instruction's first line cut to 120
characters, `body` = the instruction (trimmed), `crew` = whoever handles it; it's delegated with the
instruction as the prompt and settles like any delegation (`completed` / back to `new`). A new
alert pushes like any other. Errors match `/delegate`: `422` blank instruction or unknown `crew`,
`400` `CrewNotConfigured` — checked before anything is stored, so a failed call leaves no alert.

### `POST /api/alerts/{id}/take-to-chat`

"Talk to Hermes about this" — opens the chat **without sending anything**. Where `/delegate`
starts a run immediately, this creates the session (same crew resolution, same `alert_id`
link) and returns a `draft` for the person to read, edit and send themselves.

```jsonc
// body — all optional
{ "suggested_action_index": 0,   // draft = that suggestion's text instead of the alert's own words
  "crew": "toolshed" }           // explicit crew member; same rules as /delegate

// 200
{ "alert": { /* Alert, now status "acted" */ },
  "session_id": "01JX9…",
  "draft": "Disk is 91% full\n\nOnly 9 GB free on /var" }
```

* **Moving to the chat handles the alert**: a `new` / `snoozed` / `completed` alert becomes `acted`
  immediately, with `resolution_note: "Taken to chat"` (unless it already had one). An alert that's
  already `acted` / `dismissed` / `delegated` keeps its status.
* `draft` is the chosen suggestion's prompt, else the alert's title and body. If the alert already
  has a conversation, that session is **reused** (never a second one) and `draft` is `null` unless
  you passed `suggested_action_index`.
* The app puts `draft` in the chat's message box; nothing reaches the agent until the person sends it.
* Errors: `404` unknown alert, `422` bad `suggested_action_index` / unknown `crew`, `400` no crew
  configured. A failed call changes nothing.
* `/delegate` is unchanged — still the right call when an instruction is already in hand
  (e.g. the app's "New delegation" screen).

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
  "summary": "Can you move the dentist appointment to Friday?", // headline for the Chat list:
                                       // the title if set, else the opening message; null if empty
  "preview": "You: Done - it's now Friday at 9.",  // latest message, one line; "You: " if yours
                                       // (artifact links are left out of both)
  "last_activity_at": "2026-09-20T14:05:41Z",  // latest message's time, else created_at — show THIS
                                       // as "2m ago" in the Chat list (it's what the list is sorted by)
  "messages": [ { /* Message, GET only */ } ],

  // GET /api/sessions/{id} only — see "Opening a thread" below
  "running": true,                     // a run for this session is in flight right now
  "run_started_at": "2026-09-20T14:05:00Z",  // null when not running
  "resume_seq": 41                     // the Last-Event-ID to open the event stream with
}
```

### The Message object

```jsonc
{
  "id": "…",
  "session_id": "01JX9…",
  "role": "assistant",                  // user | assistant
  "content": "Made it: [Groceries](gaca://artifact/3f2a…)",  // raw, exactly as stored
  "display_content": "Made it:",        // what to RENDER — content minus artifact links
  "created_at": "2026-09-20T14:05:41Z",
  "attachments": [ { "id", "content_type", "size", "url" } ],   // photos, see below
  "artifacts": [                        // chips to show under the message, in link order
    { "id": "3f2a…", "title": "Groceries", "description": "Weekly list",
      "crew": "hermes", "missing": false }
  ],
  "activity": {                         // assistant only; null if the run used no tools
    "tools": [ { "name": "web_search", "count": 1, "errors": 0 },
               { "name": "terminal",   "count": 3, "errors": 1 } ],  // first-use order
    "duration_seconds": 41.3,
    "label": "Used web_search, terminal 3× · 41s"   // ready to show as-is
  }
}
```

**Clients derive nothing from message text** — every field above is computed
server-side so the Android, iPhone and web clients all show the same thing:

* **`display_content`** is `content` with every artifact link removed —
  `[label](gaca://artifact/<32 hex>)` or a bare `gaca://artifact/<32 hex>` —
  then spaces/tabs before a newline dropped, runs of 3+ newlines collapsed to
  a blank line, and the ends trimmed. Render this, not `content`. It can be
  empty when a message was nothing but a link.
* **`artifacts`** is each linked artifact once, in order of first appearance,
  resolved against the session owner's artifacts. If the link points at
  nothing of theirs (deleted, or a bad id), the chip is still there with
  `missing: true`, `title` = the link's own label (or `"Page"` for a bare
  link), and `description`/`crew` null — so a link is never silently lost.
  Open a chip with the artifact endpoints by `id`.
* **`activity`** summarizes what the agent did during the run that produced an
  assistant message: tools tallied from that run's `tool.started` /
  `tool.completed` events (`errors` = completions flagged as errors), the
  run's wall time, and a `label` — `"Ran X"` when one distinct tool, `"Used a,
  b"` otherwise, `" N×"` after a tool used more than once, then `" · "` and
  the duration (`8s`, `1m 05s`, `1h 02m`). Stored with the message when the
  run completes; `null` on user messages, on runs that used no tools, and on
  messages from before this field existed.

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

The Session plus every message in it, oldest first, and the run status fields
(`running`, `run_started_at`, `resume_seq`).

**Opening a thread.** Fetch the session, render `messages`, then open the
event stream with `Last-Event-ID: <resume_seq>`. That replays exactly the
in-flight run's events so far (nothing older — finished runs are already in
`messages`) and then continues live, so there's no guessing which replayed
progress events are stale:

* `running: false` → `resume_seq` is the session's latest event seq (`0` if it
  has none); the stream only brings new events. Show no progress UI.
* `running: true` → `resume_seq` is one before the run's first event (or the
  latest seq if the run hasn't emitted anything yet); show progress from
  `run_started_at` and fill it in from the replay.

`running` is the API's own in-process record of runs it's reading; it clears
when the run completes or fails, and a server restart clears it too (the run
dies with the process, so "not running" is then true).

### `POST /api/sessions/{id}/messages`

```jsonc
{ "content": "What's using all the disk space?",
  "attachment_ids": ["…"] }   // optional, up to 4 photos uploaded below
```

A message needs text, at least one photo, or both (`422` otherwise).
`202` immediately (`{ "turn_id": "…" }`) — this does not wait for a reply.
The user message is persisted right away; the reply streams in on the events
endpoint below and lands in the transcript when the run completes.

When the run finishes (or fails) the API pushes a notification — see §8a.

### Photos — `POST /api/sessions/{id}/attachments`

Upload each photo first, then reference the returned `id` in
`attachment_ids`. The body is the **raw image bytes** (not multipart);
JPEG, PNG, WebP or GIF, **max 2 MB** (`413` over that — the nginx layers in
front of the API enforce the same cap, so the app downscales first). The type
is sniffed from the bytes, never trusted from the header (`422` if it isn't
an image).

```jsonc
// 201
{ "id": "…", "content_type": "image/jpeg", "size": 412000,
  "url": "sessions/01JX9…/attachments/…" }   // relative to the API base
```

`GET /api/sessions/{id}/attachments/{attachment_id}` returns the bytes with
the normal bearer token (photos are private to the account; `404` for anyone
else's). Uploads that never get sent are purged after a day. A photo can be
attached to one message only.

Messages carry them back as `attachments: [{ id, content_type, size, url }]`.
Hermes sees a photo only on the turn it's sent; earlier turns' photos appear
in its history as a "[N photos attached]" note.

### `GET /api/sessions/{id}/events`

Server-Sent Events. Reconnect with `Last-Event-ID` to replay anything missed
— events are durable and sequenced, not fire-and-forget. On first open, send
the session's `resume_seq` (above) as the `Last-Event-ID`. Event types:
`assistant.delta`, `assistant.reasoning`, `tool.started`, `tool.completed`,
`run.completed`, `run.failed`, plus the unsequenced `run.heartbeat` (no id;
never replayed). This is the
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

### `GET /api/crew/{name}/profile` — how a crew member is set up

GACA's own picture of a profile, in GACA's terms — not Hermes' (each engine's
adapter translates; see `gaca_api/schemas/crew_profile.py`). Read live, never
cached. **Read-only: to change a profile, talk to it in chat.**

```jsonc
{
  "name": "default", "color": "blue", "is_default": true,   // GACA's identity for it
  "status": "online",                                       // online | offline
  "engine": "Hermes 0.20.5",
  "model": { "provider": "ChatGPT or Codex Subscription", "name": "gpt-6-luna" },
  "channels": [ { "name": "Telegram", "state": "connected", "needs_attention": false } ],
  "skill_count": 74,
  "skill_categories": [ { "name": "creative", "count": 16 } ],   // most first
  "toolsets": [ { "name": "web", "label": "Web Search & Scraping", "description": "…" } ],
  "jobs": [ { "id": "…", "name": "daily-briefing",
              "schedule": "Every day at 7:00 AM",              // already plain English
              "enabled": true, "next_run": "2026-10-01T07:00:00-04:00",
              "last_run": "…", "last_status": "ok", "kind": "agent" } ],  // agent | script
  "purpose": {                                              // what it is DEDICATED to doing
    "summary": "Toolshed software engineering, product strategy, and growth specialist",
    "summary_source": "description",                        // description | instructions | null
    "instructions": "# Toolshed Agent\n\nYou are the dedicated engineering…",  // full persona, or null
    "is_generic": false                                     // true = a general assistant, nothing specific set
  },
  "unavailable": []   // pieces it couldn't read, e.g. ["scheduled jobs", "purpose"]
}
```

**`purpose`** is GACA's own shape: `summary` is the one-line version (a written description if the
engine has one, otherwise the opening sentence of the instructions, rephrased to the third
person), `instructions` is the full standing instructions with the engine's own boilerplate
removed, and `is_generic` is true when nothing specific was ever set. **`null` means it couldn't
be read** (and `"purpose"` is in `unavailable`) — different from a generic crew member. It is read
live, never stored; for Hermes that happens over the account's direct (SSH) access to the box, so
without that configured it is always `null`. See `hermes/0002-storage-boundary.md`.

Always `200` for an existing member: an unreachable engine is
`status: "offline"` (everything else empty), and one failing piece only
appears in `unavailable`. `404` if `name` isn't one of this account's crew.

---

## 8a. Push registration — FCM (Android) and APNs (iPhone)

Both routes require the normal `Authorization: Bearer` header, same as
everything else.

### `POST /api/push/register`

```jsonc
{ "token": "…", "platform": "android" }   // platform optional, defaults "android"

// iPhone: the raw APNs device token (hex), and which APNs environment it's from
{ "token": "a1b2…", "platform": "ios", "environment": "sandbox" }
```

`environment` is `"sandbox"` | `"production"` | `null` (anything else is
`422`), and only means something for `platform: "ios"`: a debug build run
from Xcode gets sandbox tokens, TestFlight/App Store builds get production
ones, and a token sent to the wrong APNs host is rejected. Omitted/`null` on an
ios token means production.

`204` on success. Call this on every app launch and whenever the platform
hands the app a new token (Android `FirebaseMessagingService.onNewToken()`;
iOS `didRegisterForRemoteNotificationsWithDeviceToken`) — tokens rotate, so this is
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

**Routing by platform.** Tokens registered with `platform: "ios"` are sent
straight to Apple (APNs HTTP/2 provider API, token auth) — the iPhone app
needs no Firebase SDK. Every other token goes through FCM as before. A user
with both an Android and an iPhone gets both. Each side is configured on the
server independently and is a silent no-op when unconfigured:

| Server env var | |
|---|---|
| `GACA_FCM_CREDENTIALS_PATH` | Firebase service-account JSON (secret, file path) |
| `GACA_APNS_KEY_PATH` | the `.p8` APNs auth key (secret, file path) |
| `GACA_APNS_KEY_ID` | that key's id |
| `GACA_APNS_TEAM_ID` | the Apple Developer team id |
| `GACA_APNS_TOPIC` | the iOS app's bundle id; default `dev.monts.gaca` |

APNs is on only when key path, key id and team id are all set. An APNs `410`,
or `400` with reason `BadDeviceToken` / `DeviceTokenNotForTopic`, deletes that
token server-side (FCM's equivalent is "unregistered"); any other failure is
ignored.

**What an iPhone receives** (APNs payload):

```jsonc
{
  "aps": {
    "alert": { "title": "…", "body": "…" },
    "sound": "default",
    "thread-id": "alerts",                    // "alerts" | "chat" — groups them in Notification Center
    "interruption-level": "time-sensitive"    // "time-sensitive" when severity == "urgent", else "active"
  },
  "alert_id": "01JX8…", "severity": "urgent"  // alerts
  // chat replies instead carry: "session_id": "01JX9…", "kind": "chat"
}
```

Headers: `apns-push-type: alert`, `apns-priority: 10`, and `apns-collapse-id`
= the alert id (alerts) or the session id (chat), so a newer notification for
the same alert/chat replaces the older one — the iOS counterpart of Android's
one-notification-per-alert/chat. The custom keys sit at the top level of the
payload, beside `aps`, and are the same values Android gets in `data`.

**Android:** every message carries `data.alert_id` and `data.severity` (not just a title/
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
| **Android / iPhone** | Triage and chat UX. Rendering unknown `kind`s gracefully. Making which crew member you're talking to obvious (color, name) everywhere it's relevant. Local read/unread. Reconnect and refetch. **Not** parsing message text, tallying tool use, or guessing whether a run is in flight — `display_content`, `artifacts`, `activity`, `running` and `resume_seq` (§6) come from the API; nor rendering `data` (`data_rows`), resolving `artifact_id` (`artifact`), composing a delegation (`POST /alerts/delegations`), or wrapping an artifact page (`GET /artifacts/{id}/page`, `POST /artifacts/{id}/call`) |
| **frontend** | Signup, login, minting the agent token. **Not** triage |

**The app does not compute state.** Don't locally derive whether a snooze has
expired or whether a transition is legal — send the request and honour the
response. The server owns the rules; a client that duplicates them will drift.

### What gets pushed

- **Alerts** (new/delegation settled): `data` has `alert_id` and `severity`; channels `alerts_urgent` / `alerts_standard`. iPhone: same keys at the payload's top level, `thread-id: "alerts"`, collapse id = alert id, time-sensitive when urgent (§8a).
- **Chat replies** — when a normal chat turn completes the notification is the crew member's name and the reply tidied to one line (artifact links left out); if the run fails it's "<crew> couldn't finish" with the error. `data` is `{ "session_id": "…", "kind": "chat" }`; channel `chat_replies`. iPhone: same keys, `thread-id: "chat"`, collapse id = session id. The app opens that thread on tap and suppresses the notification when that thread is already on screen. Alert-delegated runs push through the alert instead, not this.
