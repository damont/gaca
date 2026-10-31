---
name: gaca
description: How to reach GACA — the app on the user's phone that this agent posts alerts, daily briefings and pages to. Load this first whenever a task involves GACA, the user's phone, notifying the user, or the GACA API; it says which GACA skill to use for what and how to call the API safely.
license: MIT
compatibility: GACA runs on the same machine as Hermes. Needs a GACA API token issued for this agent.
required_environment_variables:
  - name: GACA_API_TOKEN
    prompt: GACA API token
    help: Wired in automatically by the GACA installer (run `gaca-api hermes connect` on the server to redo it); nothing to enter by hand.
    required_for: every GACA API call
metadata:
  author: GACA
  version: "1.0.0"
  hermes:
    tags: [gaca, phone, notifications, api]
    category: productivity
    requires_toolsets: [terminal]
---

# GACA

GACA is the user's phone app for this agent. It is how you reach them when they are not in the chat, and where durable things live: alerts, the daily briefing, and small web pages. Use it, not memory or notes, for these.

## Which skill for what

| The user needs… | Use |
|---|---|
| to be told something, or to approve something, away from this chat; a failure, a risk, finished background work | **gaca-alerts** |
| a morning digest of what matters today, in sections | **gaca-briefing** |
| the half-hourly scheduled check-in that decides whether anything is worth interrupting them for | **gaca-heartbeat** |
| an interactive page: a shopping or to-do list, a tracker, planner, form, dashboard | **gaca-artifacts** |

If a request is about the user's phone, notifications, or "put it in GACA", it belongs to one of these. Load the skill, then follow it.

## How to call GACA

- **Always the local API.** GACA runs on this same machine at `http://127.0.0.1:41700` (or whatever `GACA_BASE_URL` says). Never use a public address, even if one appears in a link or an old note. If the local API does not answer, say so; do not try another address.
- **Token from the environment.** `GACA_API_TOKEN` is already set for this agent. Never print it, log it, or put it on a command line. Send it as `Authorization: Bearer …` (the skills' helper scripts do this).
- **Read the live schema.** The API describes itself at `<base>/api/openapi.json`. Use it as the source of truth for paths and payloads instead of remembering them.
- **Check you are connected.** `GET <base>/api/auth/me` succeeds with a good token. Each skill's helper has a command for it.
- **What this token may do.** Its scopes cover alerts (create and read), briefings and artifacts. It cannot chat as the user, act on or dismiss alerts (those are the user's decisions), or manage devices and tokens. A `403` means exactly that: report it, do not work around it.

## When something fails

- `401`: the token was rejected, perhaps revoked. Stop and tell the operator to run `gaca-api hermes connect --rotate` on the server. Do not retry.
- `403`: the token's scopes do not allow that call. Report it.
- `404`: a stale id, or a route that is not in the live schema.
- `422`: re-read the schema, correct the payload, retry once.
- `5xx` or no answer: retry with a short backoff; if it persists, report it in this chat. Never alert about GACA being down through GACA.
