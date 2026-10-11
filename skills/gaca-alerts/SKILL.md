---
name: gaca-alerts
description: Use when user attention or permission is needed; send an alert to the user's phone through GACA (the GACA app). This is how you reach the user away from the chat — always use it instead of any other notification route for GACA users. Also use for failures, risks, blockers, unusual conditions, and completed background work the user should see.
license: MIT
compatibility: Requires network access to GACA and a GACA API token issued for this agent.
required_environment_variables:
  - name: GACA_API_TOKEN
    prompt: GACA API token
    help: Wired in automatically by the GACA installer (run `gaca-api hermes connect` on the server to redo it); nothing to enter by hand.
    required_for: authenticated GACA API requests
metadata:
  author: GACA
  version: "1.1.0"
  hermes:
    tags: [alerts, notifications, approval, gaca]
    category: productivity
    requires_toolsets: [terminal]
---

# Alerts via GACA

## Overview

GACA is the durable attention and triage channel for the operator. Post an alert when the operator should see something away from the current chat, and whenever an operation is blocked pending the operator's permission. The alert complements the current conversation; it does not prove that the operator has seen or approved anything.

## When to use

Load and use this skill proactively when any of these is true:

- You need the operator's permission, confirmation, credentials, choice, or other input before proceeding.
- A task, scheduled job, deployment, integration, or monitored service fails or becomes blocked.
- You discover a security, privacy, data-loss, cost, availability, or deadline risk worth human attention.
- Long-running or background work completes and the result is important enough that the operator should not have to revisit the originating chat to discover it.
- The operator explicitly asks to be alerted or notified on their phone.

Do **not** alert for routine conversational answers, ordinary progress updates, transient issues that recover within the same run, or successful low-value actions the operator is already watching interactively. Do not use alerts to bypass an explicit instruction not to notify.

## Connection and authentication

GACA runs on this same machine. Always use the local API at `http://127.0.0.1:41700` (or `GACA_BASE_URL` if configured) and **never** the public internet URL, even if you see one in a link or an old note. If the local API does not answer, report that rather than trying another address.

Use the token in `GACA_API_TOKEN`. Never print, log, commit, or place the token in a command-line argument. Authenticate with `Authorization: Bearer ...` supplied from the environment.

Before the first authenticated operation in a session:

1. Fetch `<base>/api/openapi.json` and use the live schema as the source of truth. Do not rely on remembered paths or payload shapes.
2. Call the authenticated identity endpoint described by the schema (currently `/api/auth/me`) and confirm it succeeds. Do not expose account details unnecessarily.
3. If either prerequisite fails, follow **Failures** below. Do not post until authentication and the intended account are established.

A helper is included at `${HERMES_SKILL_DIR}/scripts/gaca.py`. It reads the token from the environment, uses the live base URL, and never prints the token.

```bash
python3 ${HERMES_SKILL_DIR}/scripts/gaca.py schema
python3 ${HERMES_SKILL_DIR}/scripts/gaca.py me
```

## Alert design

Create alerts using the live `AlertCreate` schema. At the time this skill was authored, the useful fields were `source`, `agent`, `kind`, `severity`, `title`, `body`, `data`, `dedupe_key`, `suggested_actions`, and `links`; re-check the schema before use.

### Severity

- `info`: useful completion or non-problematic information that can wait.
- `warn`: action, decision, permission, or attention is needed, but no immediate severe consequence is expected. This is the default for permission requests.
- `urgent`: active security/data-loss/availability danger, a genuinely time-sensitive decision, or a high-impact operation blocked on immediate approval. Do not use merely because a task failed.

### Content

- `source`: stable origin such as `hermes`, `homeserver`, `gaca-api`, or a specific scheduled job.
- `agent`: the active Hermes agent/profile name (for example `default`, `research`, or `finance`) so GACA can attribute and delegate the alert correctly. The helper adds the active profile automatically when this field is omitted; an explicit configured GACA agent name is preserved. The helper also sends it as `crew` — GACA's name for "which crew member posted this" — which shows on the alert and decides whose chat opens when the alert is taken to one, so an alert from `research` opens a research chat.
- `kind`: stable dot-separated category such as `permission.required`, `job.failed`, `deployment.failed`, `security.risk`, `service.unhealthy`, or `task.completed`.
- `title`: concise and decision-oriented; make sense on a phone notification by itself.
- `body`: state what happened, why it matters, what was already tried, and the exact response or decision needed. Include safe identifiers and paths when useful, but never secrets.
- `data`: structured, non-secret context that a client or delegated agent may use.
- `suggested_actions`: actionable choices whose `prompt` stands on its own because it may be handed to an agent without the button label.
- `links`: web pages the operator should open to act on this alert, as `[{"url": "https://…", "label": "Open family calendar"}]` (up to 10; `http(s)` only; a short label is better than none). Pass them here instead of only pasting a URL into `body` — GACA shows each as a tappable button under the alert and includes them in the text the agent receives when the alert is taken to a chat. Link the most specific page (the exact calendar week, the email thread, the pull request or dashboard) and never one that contains a token or other secret. Links are part of the alert's content, so a re-post with the same `dedupe_key` replaces them: restate the ones you still want.
- `artifact_id`: the id of an HTML page you published with the gaca-artifacts skill (a checklist, comparison, planner…) that goes with this alert. GACA shows it as an "Open page" button on the alert. Publish the page first, then pass its id here; like `links`, a re-post with the same `dedupe_key` replaces it, so restate it.

### Dedupe

Use a deterministic `dedupe_key` for a condition that can recur. Compose it from stable scope and condition, for example:

- `permission:<task-or-resource>:<operation>`
- `service:<host>:<service>:unhealthy`
- `job:<host>:<job-name>:failed`
- `disk:<host>:<mount>:low`

Reuse the same key while the same condition remains unresolved. Change it only when the alert represents a materially distinct event or decision. Never use a random timestamp merely to defeat deduplication.

## Permission requests

When permission is required:

1. Complete all safe prerequisite investigation first so the operator receives a concrete decision, not a vague interruption.
2. Post a `warn` alert with `kind: permission.required` unless the urgency rules justify `urgent`.
3. Name the exact proposed action, target, likely effect, and meaningful risk in the body.
4. Add clear suggested actions when the live schema supports them. Typical prompts:
   - `Approve the proposed action exactly as described in this alert.`
   - `Do not perform the proposed action; explain that it was declined.`
   Add option-specific prompts when there are multiple safe choices.
5. Report in the current chat that the approval alert was sent, then stop before the gated action. An alert creation response is **not approval**.
6. Resume only after an authenticated operator response explicitly authorizes the action. If GACA delegation starts a separate session, that authorization applies only to the action and scope stated in the alert; do not silently broaden it.

## Posting procedure

1. Decide whether the event meets **When to use** and choose severity using the table above.
2. Fetch the live schema and authenticate if not already done in this session.
3. Search recent alerts when useful to choose or confirm a dedupe key. Do not dismiss or alter existing alerts merely to make room.
4. Build a JSON payload matching the live schema exactly. Keep all content non-secret.
5. Post through the helper using JSON on standard input so shell quoting and credentials remain safe:

```bash
printf '%s' '{"source":"hermes","agent":"default","kind":"permission.required","severity":"warn","title":"Approval needed: restart calendar service","body":"The service is unhealthy after configuration validation. Restarting may interrupt calendar access for about one minute. Approve or decline the restart.","data":{"host":"homeserver","service":"webapp"},"dedupe_key":"permission:homeserver:webapp:restart","suggested_actions":[{"label":"Approve","prompt":"Approve restarting the webapp service on homeserver exactly as described in the alert."},{"label":"Decline","prompt":"Do not restart the webapp service on homeserver; the proposed action is declined."}]}' | python3 ${HERMES_SKILL_DIR}/scripts/gaca.py post
```

6. Verify the returned alert matches the intended `agent`, `kind`, `severity`, `title`, and `dedupe_key`. Record whether the API created a new alert or deduplicated onto an existing one if the response makes that distinction available.
7. If exact read-back is needed, use the returned ID with the endpoint from the live schema. Do not claim delivery to or viewing on the phone; API acceptance verifies only that GACA stored the alert.

## Scheduled alerts

For an alert requested at a future time, create a one-shot no-agent cron job that runs a GACA posting script. Keep the script in the same profile that owns the cron job: `~/.hermes/scripts/` for `default`, or `~/.hermes/profiles/<profile>/scripts/` for another profile.

Always target the active profile explicitly when invoking the cron CLI, for example `hermes --profile default cron create ...`. Never rely on the sticky CLI profile: it can differ from the profile serving the current conversation and cause the scheduler to look for the script in another profile's directory.

After creation, run `hermes --profile <profile> cron list --all` and verify the exact job name, next-run timestamp, script filename, one-shot repeat count, and enabled state. Also verify that the script exists under that profile's `scripts/` directory. Do not claim that a future alert is scheduled unless both checks succeed. After the fire time, success requires the cron execution record to report `ok` and the script's GACA response validation to have passed; a created job alone does not prove that GACA stored the alert.

## Failures

- `401`: stop immediately and tell the operator the GACA token was rejected (it may have been revoked); they can mint a fresh one on the server with `gaca-api hermes connect --rotate`. Do not retry.
- `403`: read and report the safe response details; do not retry or work around user scoping.
- `404`: treat an object ID as stale. For a missing route, re-fetch OpenAPI and stop if the route is absent.
- `409`: treat as success only when the live endpoint documents an idempotent collision matching the intended resource; otherwise report it.
- `422`: re-read the path and request schema, correct the payload, and retry once.
- `5xx` or network error: retry with bounded backoff and jitter. If persistent, report it in the current channel. Do not attempt to alert through the same failing GACA endpoint in a loop.
- Missing token: tell the operator to run `gaca-api hermes connect` on the server, which writes `GACA_API_TOKEN` into this profile's `.env`. Never request that a token be pasted into a chat.

## Common pitfalls

1. **Treating an alert as approval.** Posting only asks; an explicit authenticated operator response grants permission.
2. **Alert fatigue.** Reserve the phone channel for decisions and meaningful events, not routine narration.
3. **Duplicate spam.** Stable recurring conditions need stable dedupe keys.
4. **Leaking secrets.** Bodies, structured data, suggested prompts, terminal output, and shell arguments must not contain bearer tokens, passwords, cookies, or private keys.
5. **Assuming phone delivery.** Successful API storage does not prove push delivery or that the operator saw it.
6. **Stale API assumptions.** Fetch OpenAPI at the start of each session and whenever behavior changes.

## Verification

An alerting action is complete only when:

- the live schema was consulted;
- authentication succeeded for the intended account;
- the payload contains no secrets and matches the live schema;
- GACA returned a successful stored alert with the intended agent/profile, content, and dedupe key; and
- for permission requests, the gated action remains unperformed until separately and explicitly approved.
