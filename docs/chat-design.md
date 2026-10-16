# Chat design (all clients)

This is the chat interface every GACA client should build: iPhone (built) and Android (to
port). It replaces the earlier bubble-for-everything design on the Android canvas.

- **Visual reference:** the iPhone app's Chat tab. Same palette and crew colors as the rest of the app.
- **Working implementation:** `gaca-ios/Gaca/Features/Chat/` (`ChatHomeView.swift`,
  `ChatThreadView.swift`, `ChatThreadModel.swift`, `Composer.swift`).
- **API it relies on:** the sessions, messages and events routes in the live schema
  (`<your server>/api/openapi.json`). Clients stay thin. Everything below that sounds derived (activity labels,
  stripped text, resolved page cards, resume position) comes from the API. A client never
  computes it.

## 1. The Chat tab is a home screen, not a hidden drawer

From top to bottom:

1. **Large title "Chat".** Top right: search (opens History) and Settings.
2. **"New chat with":** a row of crew avatars (54 pt circles in each member's color, with
   the name under each). The selected one has a ring in its color; the others are dimmed.
   The default is the account's default crew member (`is_default`). The row is hidden when
   there is only one crew member.
3. **"Recent":** the latest ~6 sessions as list rows, with "See all" opening History. Each
   row has:
   - a 40 pt avatar
   - `summary` (bold, one line)
   - the relative time from `last_activity_at`
   - `preview` (up to two lines)
   - the crew name in its ink color, and "· 🔔 from an alert" when `alert_id` is set
4. **The composer, docked at the bottom above the tab bar.** Typing and sending creates the
   session (`POST /sessions` with the picked crew), posts the message, and pushes into the
   thread. Nothing exists on the server until that first send.

**History** is a pushed screen titled "History". It has:
- an always-visible search field
- crew filter pills ("All" plus one per member)
- sessions grouped into Today / This week / Earlier, by `last_activity_at`

## 2. The thread

**Header** (inline, centered): a 22 pt avatar and the crew name. Under it, a one-line
status while a run is in flight:
- "Using web_search…" while a tool runs
- "Working…" with no tool running and no text yet
- "Writing…" while text streams in

Otherwise the line shows the chat's `summary`. Tapping the header opens Crew detail. There
is no colored bar under the header any more.

**Your messages** are compact right-aligned bubbles:
- slate fill (`#2D3C4C`) with light text
- radius 20, tail corner 6
- max width ~300 pt
- photos stacked above the text (180×120, radius 16)

**The crew member's replies are not bubbles.** Each one is a full-width turn:
1. A small header: a 22 pt avatar and the name in the crew's *ink* color (the color
   darkened ~25%).
2. **An activity summary chip**, when `message.activity` is present. The chip shows a small
   check plus `activity.label`, e.g. "Ran terminal 3× · 41s", on a light grey pill. Tapping
   it expands a list of `activity.tools` (name in monospace, ×count, "· N failed" when
   `errors > 0`).
3. **The reply as rendered Markdown** from `display_content`: paragraphs, lists, bold,
   inline code, code blocks, headings and links. Body text is 16 pt with comfortable line
   spacing.
4. **Page cards** for `message.artifacts`. Each card is a white rounded card with:
   - a crew-tinted icon tile
   - the title and description
   - a chevron
   Tapping opens the artifact viewer. When `missing` is true, the card is disabled and
   reads "This page is no longer available".

Long-press any message to Copy.

**While a run is in flight**, the bottom of the thread shows a live turn: the same header,
then a **live activity row**. It is a pill in the crew's tint holding:
- a pulsing dot in the crew color
- "Using *tool*" (or "Working") in the crew's ink color
- an elapsed timer (12s, 1m 05s) counted from `run_started_at`

After that comes the streamed text (Markdown) with a caret. When `run.completed` arrives,
the client re-reads the session and the server's message replaces the live turn, now with
its activity chip.

**Opening a thread:**
1. `GET /sessions/{id}`.
2. If `running` is true, show the live turn immediately.
3. Open `GET /sessions/{id}/events` with `Last-Event-ID: resume_seq`. That replays exactly
   the in-flight run's events and nothing older, so the client needs no heuristics to tell
   stale events from live ones.
4. On a dropped connection, reconnect after ~2 s with the last seq seen.

**From an alert:** when `alert_id` is set, a card is pinned at the top of the thread. It
holds a severity-tinted bell tile, "URGENT ALERT · 14m" in the severity ink, the alert
title on one line, and a chevron. Tapping it opens Alert detail.

**Day dividers:** "Today 8:42 AM" (centered, small, grey) before a message that comes more
than an hour after the previous one.

**Composer:** a floating white capsule (radius 26, hairline border, soft shadow) holding:
- `+`, which opens the photo picker. Up to 4 photos, shown as 64 pt thumbnails with a
  remove badge.
- a growing text field (1–5 lines), with placeholder "Message *Name*…"
- a round send button: crew color when sendable, grey otherwise, a spinner while sending

A sent message shows dimmed right away until the server's copy arrives. Errors (a failed
send, or `run.failed`) show as one red line above the composer.

**Empty thread:** "Say hello to *Name* to get started."

## 3. What changed from the Android chat, and why

| Before (Android) | Now | Why |
|---|---|---|
| Assistant bubbles in solid crew color, user bubbles grey | Replies are prose; your bubbles are slate | Long answers, lists and code read like a document; color stays on identity (avatar, ink, accents) instead of flooding the screen |
| Plain text, no Markdown | Markdown rendered | Agents answer in Markdown |
| Tool activity only as a header subtitle, then lost | A live activity row while working; afterwards a persisted, expandable activity chip from the API | You can see what was done, even later |
| Artifact links parsed out on the client | `display_content` and `artifacts[]` come from the API | One rule for every client |
| A replay heuristic to ignore stale progress events | `running` / `resume_seq` from the API | No guessing |
| The Chat tab was a blank draft behind a drawer | A home screen with crew picker, recent chats and composer | Everything is one tap away; no hidden drawer |

## 4. Tokens

| Name | Value |
|---|---|
| Page | `#F0F4F9` |
| Surface | `#FFFFFF` |
| Slate (accent, user bubble) | `#2D3C4C` |
| Text | `#282F35` |
| Secondary text | `#5A6169` |
| Meta text | `#747B83` |
| Border | `#C8CED5` |
| Activity pill | `#E3E8EE` |
| Crew tint | the crew color mixed 80% toward white |
| Crew ink | the crew color mixed 25% toward black |
| Crew solid (buttons) | the crew color mixed 12% toward black |
