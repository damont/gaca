# Alert actions and the simpler alert lifecycle

Status: in progress (started 2026-11-16). This is the plan; the PRs below implement it. Part 2 (this section's "The API owns the workflow") added 2026-11-17.

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

## The API owns the workflow; apps display it

Added after the Android design. An app must not decide anything about an alert. It renders what the
API sends and posts the person's taps:

- `AlertOut.actions`: every action with `state` (`idle` / `running` / `done` / `failed`), the `label` to
  show now (an "Add all" offers only the rest once some are done), `detail` (the line under it, built
  from `params`), `cta` (the button word), `prominent` (the big "Add all" button), `can_run`, `note`
  while running, and `result` (summary, links, or a draft).
- `AlertOut.workflow`: `phase` (`open` / `working` / `waiting_on_you` / `closed`), the status line, the
  heading over the actions, "1 of 3 added", how "chat about this" reads (and whether it is the main
  button), the close buttons (`close_options`: reason, label, primary) with their heading and hint, and
  why a closed alert closed.
- Taps: `POST /alerts/{id}/actions/{index}/run`, `POST /alerts/{id}/close {reason}`,
  `POST /alerts/{id}/take-to-chat`. `services/alert_view.py` is the one place these rules live.

Rules decided there:

- An action closes the alert when it succeeds and every closing action is done (so adding one of three
  events leaves two to tap). `email.draft` actions never close the alert.
- An "Add all N" action lists `covers` (the actions it stands in for) and a `remaining_label`; once some are
  done it offers, and tells the agent to do, only the rest.
- A draft is stored whole (To, Subject, body) on the alert and on the run, with a ready-made `mailto:` link and
  copy text. Hermes only reads mail, so GACA never sends: the person opens the draft in their mail app.
  Asking for changes in the alert's chat replaces the draft (a chat reply that carries a `To:`/`Subject:`
  draft is split into text plus a draft card, and the latest one becomes the alert's draft).
- With a draft waiting, the close buttons become "I sent it" (closes `done_by_gaca`: Hermes helped), "I'll write
  it" (`done_by_me`) and "No reply needed" (`not_useful`). `done_by_gaca` may be chosen by hand only once an
  action has succeeded.
