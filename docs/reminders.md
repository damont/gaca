# Reminders

Status: agreed 2026-11-22. PRs 1 to 3 below are done (design, skill sync, API); the rest are not built yet.

## Why

People say "remind me to call the dentist tomorrow", and agents notice things that carry a date
(a bill, a package, an event) and could offer "want a nudge Thursday?". Today there is nowhere for
that to live except a Hermes cron job per reminder, which is how one-off reminder jobs pile up on a
Hermes box: opaque, invisible in the app, and tied to Hermes.

The goal is short, near-term memory that works in the background. The person should not manage a
reminders screen. They say it (or accept a suggestion), and later it shows up where alerts already do.

## Decisions

- **The API owns reminders.** A `reminders` collection in the GACA database is the source of truth.
  Hermes does not store them in its own memory or cron.
- **Hermes reaches them through a skill**, `gaca-reminders`, like `gaca-alerts` and `gaca-briefing`.
- **The existing heartbeat drives delivery.** No new scheduler (see "Delivery" for the limits).
- **A reminder that fires is an alert.** Same inbox, same push, same close choices.
- **Single owner only.** No shared reminders, no "remind both of us".
- **No recurring reminders and no location triggers**, for now.
- **No new tab.** The only new UI is a quiet "Scheduled" list (see "In the app").

## The model

A reminder is small:

| Field | Meaning |
|---|---|
| `id`, `owner` | as everywhere else |
| `text` | what to remind them of, in the agent's words, short |
| `due_at` | UTC instant, or null for a "later" note |
| `explicit_time` | true when the person gave the time ("at 10"), false when the agent chose one ("tomorrow", "later") |
| `state` | `pending`, `fired`, `done`, `cancelled`, `expired` |
| `source` | `{kind: chat or alert or agent, crew, session_id?, alert_id?}`: who made it and where from |
| `expires_at` | notes only: when an unfired "later" note stops being worth keeping (default 30 days) |
| `fired_at`, `late` | when it fired, and whether it fired more than 30 minutes after `due_at` |
| `alert_id` | the alert created when it fired |
| `created_at`, `updated_at` | |

Times are stored in UTC. The skill turns "tomorrow at 10" into a UTC instant using the user's
timezone from the heartbeat settings, which the API already holds. The API never parses natural language.

### Two kinds

- **Timed reminder** (`due_at` set): fires as an alert at that time.
- **"Later" note** (`due_at` null): never pings. It is surfaced in the next daily briefing under "loose
  ends", and attached as context to agent turns (open notes only, newest first, capped) so the agent can
  say "you wanted to look into X". It is how "remind me sometime" works without a notification.
  Notes expire so they cannot pile up.

## API

Plain REST under the existing token auth, scope `reminders` (in the default token scopes; tokens minted before it
existed need re-minting or the scope added, or the skill gets a 403):

- `POST /api/reminders`: `{text, due_at?, explicit_time?, source?, expires_at?}`. Before creating, the API
  returns the existing pending reminder when the text and time are near-identical (the agent can also check
  first); the response says `duplicate: true` and nothing new is made.
- `GET /api/reminders?state=pending`: for the app's Scheduled list and for the skill.
- `PATCH /api/reminders/{id}`: reschedule (`due_at`), edit `text`, or `state: cancelled`. Used by the app's
  "Undo" and by the reschedule presets.
- `POST /api/reminders/fire-due`: fires everything due now and returns what it fired. This is what the
  heartbeat calls. It is idempotent: a reminder is claimed with one atomic update (`pending` to `fired`),
  so two overlapping runs cannot both fire it.

Presets (in an hour, this evening, tomorrow morning) are computed by the API from the user's timezone and
quiet hours, so Android and iOS only render them, the same rule as close options in
`services/alert_view.py`.

## Delivery: the heartbeat

The heartbeat is the Hermes cron job that runs every 30 minutes on the default profile. Its skill gets
one new first step, before the gate:

1. `heartbeat.py reminders` calls `POST /api/reminders/fire-due`. It is deterministic: no judgment, no model
   choice about whether to fire.

Firing a reminder means the API creates an alert (title is the text, source is the crew member who made
it, close choices Done, Glad to know, Not useful, plus the reschedule presets as per-item actions the API
runs itself) and sends the push through the existing FCM and APNs paths. Hermes never posts the alert, so
a reminder does not depend on the heartbeat model run succeeding, only on the heartbeat waking up.

It runs **before the gate** because the gate suppresses runs for the user's quiet hours, pauses, the daily
run budget and after a provider ran out of credit. Those are decisions about whether the *heartbeat's own
looking around* should happen. They do not apply to a thing the person asked to be reminded of. Rules
inside `fire-due`:

- **Explicit time** (`explicit_time` true): fire when due, even in quiet hours. They asked for 6:30.
- **Vague time** (false): hold until quiet hours end and any pause the person set in the app has passed.
- **Missed during downtime:** a reminder past due fires on the next tick and is marked `late`; the alert text
  says "was due 10:00".

### Limits we are accepting, and the escape hatch

- **Precision is the heartbeat's half hour.** "At 10:00" can arrive at 10:30. The skill tells the person
  the real time in its confirmation ("Reminder set for 10:00, delivered within half an hour"), and the
  presets round to the half hour.
- **It needs the Hermes cron job to run.** A provider that is out of credit stops the job from running the
  model at all. If that proves a problem in practice, a small API-side timer that calls `fire-due` itself is
  about twenty lines and does not change anything else in this design.
- **Only the default profile has a heartbeat** (see "Housekeeping"), so reminders are delivered by the default
  profile's heartbeat no matter which crew member created them.

## Creating reminders

1. **In chat.** "Remind me to call the dentist tomorrow at 10." The agent already knows the date and the
   user's timezone, calls `gaca-reminders create`, and replies with one line: "Reminder set for tomorrow at
   10:00." The chat shows it as a small chip with Undo (`PATCH state: cancelled`).
2. **From an alert.** A poster that writes an alert containing a date or a deadline (a bill, an event, a
   package) may attach one suggested reminder as an ordinary alert action: "Remind me Thursday 9am". One tap
   runs it (`source.kind: alert`, linked to the alert). Posters must not add a generic "remind me later" to
   every alert. That is snooze again, and snooze was removed because nobody used it.
3. **By the agent on its own**, via the same skill, labelled with who made it. The skill checks for a
   similar pending reminder first.

## In the app

- **Nothing new in chat or the alert list.** A fired reminder is just an alert.
- **A quiet "Scheduled (3)" row** at the bottom of the Alerts tab (and on Today) opens a plain list of
  pending reminders and notes, each with swipe-to-cancel and a tap to reschedule. Hidden when empty.
- Android and iOS render what the API sends; the API supplies labels, presets and the "was due" wording.

## Hermes side

### Skill

`skills/gaca-reminders/SKILL.md` plus `scripts/reminders.py` (create, list, cancel, reschedule), following the
layout of the other GACA skills. The description names the trigger phrases ("remind me", "don't let me
forget", "follow up on this later", "tomorrow at 10") because that is how Hermes decides to load a skill. The
body holds the rules: convert to UTC with the user's timezone; set `explicit_time` only when the person gave
a time; check for duplicates; confirm in one line; no `due_at` means a "later" note.

### Standing instruction (SOUL.md)

SOUL.md should not list skills, because a list goes stale and competes with the skill descriptions. It
should carry one short line, the same for every crew member:

> You are built to work with GACA, the app your person uses. Use the GACA skills and API whenever they can
> do the job (alerts, reminders, artifacts, the briefing) instead of working around them or just promising
> to remember.

It goes in a delimited block, `<!-- gaca:begin -->` to `<!-- gaca:end -->`, written by the same managed sync
that installs skills (so it can be updated everywhere at once, and edits outside the block are never touched).
`translate_purpose.custom_instructions` strips the block, so the crew profile screen still shows only what
the person wrote under "dedicated to".

### Existing reminder-shaped cron jobs

An install that has been in use may already have Hermes cron jobs that are really one-off reminders. They
keep working and are not migrated automatically. Going forward the agent creates new ones through the
skill; a one-off pass can move the pending ones across.

## Housekeeping this surfaced

1. **`gaca-lists` is gone.** Lists left the API, and the skill left the repo. A check of the live Hermes install on
   2026-11-22 found no `gaca-lists` skill files in any profile, because the managed sync removes skills that
   no longer exist upstream. What is left is a stale `gaca-lists` entry in Hermes's own
   `skills/.usage.json` (Hermes's file, harmless, not ours to edit) and old session logs. The README and
   `gaca` skill should be checked for stray mentions when the next skills PR goes out.
2. **The heartbeat skill belongs to the default profile only.** The cron job is already created only on the
   default profile (`provisioning/heartbeat_job.py`), but `skill_sync` copies every skill into every profile,
   so `gaca-heartbeat` currently sits in every other profile where it can never run
   meaningfully. A heartbeat for another profile would be a different job entirely (it would watch that
   crew member's own domain, not the person's mail and calendar), so it should be written when needed, not
   inherited. The sync takes a per-skill "default profile only" rule, and removes an unmodified copy from other
   profiles by the manifest it already keeps.

## PRs

1. **This design.**
2. **Skill sync: default-only skills, and the SOUL.md block.** Small, no reminders yet. Removes
   `gaca-heartbeat` from non-default profiles.
3. **API: reminders table, endpoints, `fire-due`, fire as alert, push.** Tests for atomic claiming,
   quiet-hours rules, `late`, duplicates, owner isolation.
4. **Hermes skills: `gaca-reminders`, and the heartbeat's first step.** Ships with the deploy.
5. **Android and iOS: the Scheduled list, Undo chip, suggested-reminder action, presets.**
6. **Later notes in the briefing and as agent context.**
7. **Later, if wanted:** recurring reminders, an API-side timer, per-profile heartbeats.
