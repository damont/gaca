# Efficiency: spend fewer model tokens on the same work

Agreed with David 2026-11-28. Most of a crew's tokens go to scheduled jobs, not chats. On
david-claw, over 30 days, 97% of ~390M tokens were scheduled runs, and two half-hourly jobs were
87% of everything. Most of those runs woke the model, read a 20–35k-token prompt and ended in
`[SILENT]`. GACA fixes its own share directly and helps the owner fix the rest, once a week.

## 1. The heartbeat's pre-run (GACA's own fix)

Hermes runs a job's `script` before the model and **skips the model entirely** when the script's
last line is `{"wakeAgent": false}`; any other output becomes context for the run.

- `skills/gaca-heartbeat/scripts/heartbeat.py prerun` does, without the model: fire due reminders,
  the gate (switch, pause, quiet hours, run budget), a cheap look at the newest mail through the
  google-workspace skill's helper (ids, senders and subjects only), and the alert context.
- It skips the model when the gate says no, or when no new mail arrived since the model last looked
  and that look was less than `full_look_minutes` ago (default 120, so the calendar and the watch
  list still get a look every two hours). If mail can't be checked, it always wakes.
- When it wakes the model, its output tells the model steps 0–2 are done and lists the new mail, so
  the model goes straight to judging (no reloading skills or re-running the gate).
- `gaca-api hermes connect` (and the installer) writes `gaca-heartbeat-prerun.py` into the default
  profile's `scripts/` (Hermes only runs scripts from there) and attaches it to the job: on a new
  job at creation, on an existing job only if it has no script of its own. The shim hands over to
  the skill's helper, so the logic ships and updates with the skill. A file of that name GACA didn't
  write is never overwritten. Any failure in the shim wakes the model, as before.

## 2. Usage (`GET /api/usage?days=7`)

gaca-api reads each crew member's Hermes `state.db` read-only (`provisioning/usage_read.py`, same
pattern as `cron_runs.py`; over SSH the same file runs on the box) and its `cron/jobs.json`.
Sessions are grouped per scheduled job (Hermes names them `cron_<job id>_<date>_<time>`) and per chat
source. A run is "silent" when its last assistant reply is `[SILENT]`. The report has every line
with its share and the week before, a pay-per-token comparison (config `usage_compare_*`, default
MiniMax-M3.1-Flash on Nebius Token Factory, Nov 2026 prices) and the findings. App only: no agent
token scope reaches it.

## 3. The rules (no model, `services/usage.py`)

Per automation, per week, at most one finding, at most four in all, biggest saving first:

| rule | when | suggestion |
|---|---|---|
| `heartbeat_prerun` | the default profile's `gaca-heartbeat` has no script | operator note: run `gaca-api hermes connect` |
| `prerun` | no pre-run yet, ≥14 runs, ≥60% silent, ≥5% of all tokens | add a pre-run check |
| `review` | ≥25% of all tokens, or ≥40k tokens per model call and ≥5% | propose savings (no change) |

## 4. The weekly alert (`services/efficiency_watch.py`)

A background task checks hourly; the review runs once per ISO week on `efficiency_weekday` (0 =
Monday) at or after `efficiency_hour` in the owner's time zone, or later that week if missed. One
alert, kind `efficiency.weekly`, crew `default`, dedupe `efficiency:<year>-W<week>`; nothing is
posted when there's nothing to say. The body: total tokens and trend, the top three, the
pay-per-token monthly estimate, last week's results, operator notes. One button per finding:

- **Add a pre-run check to X** (`efficiency.prerun`, closes): the crew member reads the job, writes a
  cheap script that checks the same thing, tests it and attaches it with the skill's helper.
- **Suggest savings for X** (`efficiency.review`, does not close): a proposal only.
- **Undo the change to X** (`efficiency.undo`): offered the week after a pre-run check that cut
  that job's tokens by less than 20%.

`POST /api/usage/review` runs it now (for testing, or a future "check now" button).

## 5. Making changes (`skills/gaca-efficiency`)

The button's prompt names the skill, the profile and the job. The skill's rules: never change what a
job does (prompt, schedule, delivery, skills) unless the button says so; back up first; change only
through `hermes cron edit`; when unsure, wake the model. Its helper `efficiency.py`:
`show`, `backup`, `attach` (tests the script once, backs up, copies it into that profile's
`scripts/` as `efficiency-gate-<job>.py`, attaches it; refuses to drop an existing context script
without `--replace`), `undo` (restores script, model, provider, schedule, prompt, skills from the
latest backup), `backups`. Backups: `~/.hermes/gaca-efficiency/backups/`. The alert's chat runs on
the default profile, which can edit any profile's jobs on the same box.

## Not done (later)

- An app screen for `/api/usage` (the alert covers it for now).
- Settings in the app for the review day and the comparison prices (config only today).
