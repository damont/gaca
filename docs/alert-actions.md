# Alert actions and the simpler alert lifecycle

Status: in progress (started 2026-11-16). This is the plan; the PRs below implement it.

## Why

Acting on an alert meant going to a chat and re-asking for it. Take-to-chat pasted the whole
alert into the message box, so picking one thing out of several (one calendar event of three)
meant editing text. Snooze, delegated and completed existed as alert states, but nobody used
snooze and "delegated" is something that happens to an alert, not something an alert is.

## The model

**An alert is open or closed.** Open is stored as `new`. A closed alert says how it ended:

| Status | Meaning | Set by |
|---|---|---|
| `new` | Open | - |
| `done_by_gaca` | An action ran and succeeded, or it was taken to chat | automatic |
| `done_by_me` | I handled it myself | me, when closing |
| `noted` | Useful, nothing to do | me |
| `not_useful` | I don't want this | me |

`done_by_me` is the gap signal (what an action could do for me). `not_useful` versus `noted`
is the quality signal the heartbeat reads. Snooze is removed.

**Running an action is a run, not a status.** Each tap creates a run on the alert
(`running`, `done` or `failed`, with a short summary, links, and the session that did the work).
The alert stays open while a run is in flight and when one fails (with a retry). A successful run
closes the alert as `done_by_gaca`.

**Actions are per item.** The poster (Hermes) writes one self-contained action per thing
("Add 'Dentist, Thu 3pm'") plus an "Add all" when it makes sense. An action has `kind`, `label`,
`prompt` and optional `params`. Actions are one-shot: once one has succeeded the alert is closed.

**Results come back on the alert**, inline under the action. The session behind a run is the
transcript, opened only on demand ("continue in chat").

**A chat tied to an alert always gets the alert.** The API attaches the alert (title, body,
data, links, actions, run results) to every agent turn in a session that has an `alert_id`. The
message box starts empty, or holds only the one-line action the person picked.

## Migration of stored alerts

Rows are normalised on read, so nothing needs rewriting:
`snoozed`, `delegated`, `completed` become `new`; `acted` becomes `done_by_gaca` if a chat or run
was involved, otherwise `done_by_me`; `dismissed` becomes `not_useful`.

## PRs

1. **API** (`alert-actions-api`): states, runs, per-action endpoint, close endpoint, chat context,
   heartbeat/gaca-alerts skill vocabulary.
2. **Hermes posters**: per-item actions for the email-to-calendar suggestion alert (the pilot),
   then the other posters. Lives on the Hermes box, so it ships with the deploy.
3. **Android**: action list with inline results and the four close choices; remove snooze, delegated
   and completed UI; take-to-chat no longer prefills the alert text.
4. **iOS and launcher**: same, after `gaca-sync` shows the drift.
5. **Cleanup**: remove deprecated `act`/`dismiss`/`delegate`/`snooze` routes once the apps are updated.
