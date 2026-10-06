---
name: gaca-artifacts
description: Build interactive HTML pages for the user in GACA — a Costco list they can tick off, a trip planner, a tracker, a comparison, a form, a dashboard — that open in their app, keep their data, and that you can read and update later. Use when the user asks for a page, tracker, planner, checklist with custom behavior, or anything that needs buttons, inputs or a layout a chat message can't give; or when you want to show something richer than text in a chat or alert. Not for plain shopping/gift/to-do lists (use gaca-lists).
license: MIT
compatibility: Requires network access to GACA and an operator-issued bearer token.
required_environment_variables:
  - name: GACA_API_TOKEN
    prompt: GACA agent bearer token
    help: Generate a token from the Agent tokens section of the GACA profile page.
    required_for: publishing pages and reading or writing their data
metadata:
  author: GACA
  version: "1.0.0"
  hermes:
    tags: [artifacts, html, pages, trackers, planners, gaca]
    category: productivity
    requires_toolsets: [terminal, file]
---

# Artifacts

An **artifact** is an HTML page you write that opens inside the user's GACA app, plus the data it keeps. The user finds it in the app's **Artifacts** tab, or taps a chip in a chat or alert. The page is only a *view*: everything it remembers is stored in GACA, so the user's entries survive, you can read and change the same data while the page is closed, and you can rewrite the page without losing anything.

Helper: `python3 ${HERMES_SKILL_DIR}/scripts/artifacts.py <command>` (run it with no arguments for the full list). It reads the token from the environment and never prints it. **Pass everything as plain arguments or files** — don't pipe, heredoc or `-c` anything into it (unattended runs block that). To publish, write the page to a file first with your file tool, then pass `--html-file`.

**Read `references/sdk.md` before writing a page** — it is the contract for `window.app` (reading, writing, events) and the rules every page must follow (self-contained, mobile-first, app theme variables, no browser storage). Start from `templates/checklist.html` when the page is list-shaped.

## Artifact or list?

- A plain shopping, gift or to-do list → **use gaca-lists**, not this. It's faster, and chat can edit it.
- Use an artifact when the user **asks for a page/tracker/planner/dashboard**, or wants something custom (categories with progress bars, a packing list per person, a meal planner, a side-by-side comparison, a form, a countdown, a checklist with notes and photos-by-link). If the user says "make me a … page" or "an artifact", make one even if it's list-shaped.
- A one-off richer answer (a comparison table, a schedule) can be a read-only artifact with no stored data.

## Making a page

1. **Decide the data shape first.** List-like things → a keyed collection (`items`, `entries`, `tasks`), one document per thing. Single values and settings → `state`. Write it down in `--data-notes` (one or two sentences, e.g. `items: {text, done, category?, quantity?}`) — that note is what you, or another crew member, read when you rewrite the page later.
2. **Write the page** to a file (e.g. `/tmp/costco.html`), following `references/sdk.md`. Keep it one file, mobile-first, using the theme CSS variables.
3. **Seed data** if it should open with content: `--collection items=/tmp/items.json` where the file holds `{"milk": {"text":"Milk","done":false}, ...}` (document id → document), or `--state-file`.
4. **Publish:** `publish --title "Costco" --html-file /tmp/costco.html --description "Weekly run, by aisle" --data-notes "items: {text, done, category?}" --collection items=/tmp/items.json`.
5. **Tell the user**, and include the printed `link` (`gaca://artifact/<id>`) in your reply — the app turns it into a tappable chip. Say what the page does in a sentence. Don't paste HTML into the chat.

Titles are short and nicely cased (`Costco`, `Denver trip`), one page per purpose — look first (`list`) and **update the existing page rather than making a near-duplicate**.

## Changing a page or its data later

- **Change the data the user would expect you to change** ("add paper towels to the Costco page"): `add <ref> items --json '{"text":"Paper towels","done":false}'`, `patch`, `remove`. Check the page's shape first with `show <ref>` (it prints `data notes` and what's stored) and `docs <ref> items`; match the existing document shape exactly, or the page won't render the new entry properly.
- **Change the page itself:** `html <ref> --out /tmp/x.html`, edit the file, then `update <ref> --html-file /tmp/x.html --note "added aisle grouping"`. The stored data is untouched, so **the new page must keep reading the same data shape**. If the shape has to change, migrate the stored data first (`patch`/`state-patch`), then update `--data-notes`. Old versions are kept: `versions`, `restore`.
- Don't delete a page unless the user asks. `archive` tidies it away; `delete` removes it **and its data** (the helper asks you to confirm with the user first).

## Hearing back from the page

Pages can call `app.emit(name, payload)` ("checked-out", "picked-friday"). Read them with `events <ref> --unhandled`, act on them, then `handled <ref> <seq>`. When you build a page that expects you to react to something, say in your reply how the user can get you to — `events` isn't pushed to you; you see it when you're run (a cron job, or the user messaging you). If a reaction needs to be prompt, use `app.ask(...)` instead, which opens a chat with a draft the user sends.

## Showing a page on a chat or an alert

- **Chat:** put the page's `link` in your reply. That's all.
- **Alert:** post the alert with the field `"artifact_id": "<id>"` (the david-alerts helper passes it through). The alert then shows an "Open page" button. Publishing with `--alert <alert id>` also links the page back to an existing alert.

## Failures

- A command exits **3** = decide first (no/several matches, or a delete to confirm). Ask the user; never guess.
- **409 / CONFLICT** = someone changed it meanwhile. Re-read (`docs`, `state`) and apply your change again.
- 401/403: stop and tell the user the GACA token needs renewing. Any other error: report it plainly; never pretend a page was published.
