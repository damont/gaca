---
name: gaca-efficiency
description: Make the user's scheduled jobs cheaper in model tokens without changing what they do, when GACA's weekly efficiency review asks you to (a button on an "Improve your efficiency" alert), or when the user asks why their usage is high or how to cut it. Covers adding a pre-run check that skips the model when there is nothing to do, proposing savings, and undoing a change.
license: MIT
compatibility: Runs on the Hermes box itself; needs the terminal and the hermes command. Changes go through `hermes cron edit` only.
metadata:
  author: GACA
  version: "1.0.0"
  hermes:
    tags: [efficiency, tokens, cost, cron, gaca]
    category: productivity
    requires_toolsets: [terminal]
---

# Efficiency

## What this is for

Once a week GACA reads how many model tokens each scheduled job used and posts one alert with a button per suggestion. When the user taps one, you are asked to do it. The point is to spend fewer tokens on **the same work**: the user must not notice any difference in what their jobs do, except that they cost less.

Helper: `python3 ${HERMES_SKILL_DIR}/scripts/efficiency.py <command>` (`-h` for all): `show`, `backup`, `attach`, `undo`, `backups`. Every command takes `--profile <name>` (the Hermes profile that owns the job, `default` for the main one) and `--job <id>`. It works on any profile on this box, so you can fix a job of another crew member from here.

## The rules

- **Never change what a job does**: not its prompt, its schedule, where it delivers, or which skills it uses, unless the button explicitly says so. A pre-run check is additive: it only decides whether the model needs to wake up.
- **Back up before any change.** `attach` does it for you; for anything else, run `backup` first.
- **Every change goes through `hermes cron edit`** (the helper does this). Never edit `jobs.json` by hand.
- **When unsure, wake the model.** A check that skips a run it should have done is worse than the tokens it saves. If you can't make the check reliable, say so and change nothing.
- Reply in a few lines: what you changed, when the job still wakes the model, and how to undo it (`efficiency.py undo --profile … --job …`).

## Adding a pre-run check (the usual button)

Hermes runs a job's script before the model and **skips the model entirely when the script's last printed line is `{"wakeAgent": false}`**. Any other output is handed to the model as context for that run.

1. `efficiency.py show --profile P --job ID`: read the prompt, the skills it loads, and any script it has. Work out what the job is really waiting for ("a price moved more than 3%", "a new email from the school", "a new video on the channel").
2. Write a small, cheap script that checks **that same thing** without the model: call the same API or command the job's skill uses, compare with what it saw last time (keep a small state file next to the script, e.g. `$HERMES_HOME/gate-state/<job id>.json`), and:
   - nothing new: print a one-line reason, then `{"wakeAgent": false}` as the very last line;
   - something new: print what changed (it becomes context the model sees), and make the last line anything else, e.g. `{"wakeAgent": true}`.
   - Any error, timeout or doubt: wake (print the error, no `false` line). Keep it under a few seconds; standard library Python is best.
   - Respect anything time-based in the job (market hours, a daily summary at a set time): if the job must always act at a certain time, the script must wake then.
3. Save it somewhere temporary (e.g. `/tmp/gate-<job id>.py`) and run `efficiency.py attach --profile P --job ID --file /tmp/gate-<job id>.py`. It tests the script once, backs the job up, copies it into the profile's `scripts/` folder and attaches it. If the test exits non-zero, fix it and try again.
4. If the job **already has a script** (its output is context the model reads), do not drop it: write one script that prints what the old one printed and then the wake line, and attach it with `--replace`.

## "Suggest savings" (a proposal, not a change)

Look at the job (`show`), and reply with the two or three changes that would cut the most tokens and what the user would notice, if anything. Candidates, roughly in order of value:

- **A pre-run check** (above), when many runs end with nothing to report.
- **Fewer skills loaded**: every skill attached to a job is sent with every model call.
- **A smaller model pinned to this job** (`hermes cron edit <id> --model <name> --provider <provider>`): fine for simple, repetitive checks; never for anything that writes to the user in their name.
- **A gentler schedule**: only when the job clearly runs more often than its purpose needs, and only with the user's yes, because it changes behaviour.

Do not make the change unless the user says so in the chat that follows.

## Undo

`efficiency.py undo --profile P --job ID` puts the job back exactly as the latest backup had it (its script, model, schedule, prompt and skills). `efficiency.py backups` lists them; `--backup PATH` picks an older one.

## GACA's own heartbeat

The `gaca-heartbeat` job has its own pre-run script (`gaca-heartbeat-prerun.py`) that GACA installs and keeps up to date. Don't attach a different one to it; if it is missing, the fix is for the operator to run `gaca-api hermes connect` on the server.
