# The page SDK (`window.app`)

Every artifact page gets `window.app` before its own script runs. **A page is a stateless view:** it renders what is in the API and writes changes back through `app`. It has no network (fetch/XHR/WebSocket/iframes are blocked), no cookies or browser storage, and no access to anything but its own data. That is what makes it safe to run a page you just wrote.

## Page rules

- **Self-contained.** One HTML file: inline `<style>` and `<script>`, no CDN scripts, no external fonts, no remote images. Inline SVG, canvas, CSS and `data:` URIs are fine. Charts: draw with SVG/canvas yourself.
- **Mobile first.** It opens full-screen on a phone. No hover-only UI, no horizontal page scrolling (a wide table scrolls inside an `overflow-x:auto` box), no fixed pixel widths. `<meta viewport>` is already set. Rows are compact (about 32px), but **the whole row is the tap target**, not just a small checkbox or icon.
- **Use the app's look, and don't restyle it.** The page already ships a stylesheet (see *Look* below): plain elements are styled, and a few classes cover the repeated shapes. Colors are CSS variables (`--bg`, `--text`, `--muted`, `--faint`, `--accent`, `--on-accent`, `--border`, `--rule`, `--danger`, `--ok`, `--radius`, `--font`, `--mono`); never hard-code a color, because the same variables flip for dark mode.
- **Never size a page with `vh`, `dvh`, `svh` or `height: 100%`.** In the Android app's WebView they all resolve to **0**, so a full-screen page collapses to nothing and shows blank. The SDK keeps a CSS variable `--app-h` (the real height of the page area, in px, updated on resize and keyboard): use `height: var(--app-h, 600px)`. Width units (`vw`, `100%` width) are fine. Pages that just flow and scroll don't need any of this.
- **No title, no intro.** The app shows the title above the page. Do not start with an `<h1>`, a subtitle, a description or "how to use this". The page opens on its content and must be self-explanatory. The one-line description goes in `--description` at publish time and nowhere else.
- **Never use `localStorage`/`sessionStorage`/cookies.** They are off; if touched they are a throwaway in-memory stand-in that loses everything when the page closes. Anything the user would be upset to lose goes through `app.*`.
- **Escape anything you put into `innerHTML`** (item text is user data). The template has an `esc()` helper.
- **Render from data, not from clicks.** On a click, write with `app.*`; `app.subscribe` re-renders when the change lands. That is also how an agent's edits show up. Don't rebuild a form while the user is typing in it.

## Reading

```js
app.subscribe(function (snap) { ... })   // now, and on every change (also agent edits); returns unsubscribe
app.load().then(function (snap) { ... }) // one read
```

`snap` is `{ state, stateVersion, collections, rev, docs(name) }`:
- `snap.state` — the one state object (see below).
- `snap.docs('items')` — the documents of a collection as plain objects `{id, ...fields}`, oldest first (`[]` if none).
- `snap.collections.items` — the same with metadata: `[{id, version, createdAt, updatedAt, data}]`.

## Writing — pick the shape that fits

**Keyed collections** — for lists of things (items, entries, tasks, expenses). Each document is its own row, so the user and the agent adding at the same moment never overwrite each other. **Prefer this for anything list-like.**

```js
var items = app.collection('items');       // names: lowercase letters, digits, - and _
items.add({ text: 'Paper towels', done: false })     // → {id, version, data}; pass a 2nd arg to choose the id
items.set(id, { ...whole document... })              // create or replace
items.patch(id, { done: true })                       // merge fields; null removes a field
items.remove(id);  items.clear();  items.list();
app.batch([{ op:'set'|'patch'|'delete'|'clear', collection:'items', id:'x', data:{} }, ...])  // all or nothing
```

**State** — one object, for settings and small single-value pages (a counter, a chosen date, a form's answers). Whole-page last-write-wins unless you pass `{ifVersion: snap.stateVersion}`.

```js
app.state.patch({ chosen: 'Friday' })     // merge (objects merge, null removes a key, arrays replace)
app.state.set({ ... });  app.state.get()
```

Writes return promises; a failed write rejects with an `Error` (`err.status` is the HTTP code; 409 means someone else changed it — re-read and retry).

## Schemas (what a document must look like)

A collection can have a schema. GACA checks every write against it — the page's and the agent's, adds, replaces, merges and batches, and the seed data at publish time — and refuses a bad document with a `422` that names the problem (`items/milk doesn't match its schema: 'done' must be true or false, got 'yes'`). In a page, a refused write rejects its promise with that message (`err.status === 422`), so catch it and `app.toast(err.message)`.

```json
{
  "items": {
    "fields": {
      "text":     {"type": "string", "required": true, "max_length": 200},
      "done":     {"type": "boolean"},
      "quantity": {"type": "integer", "min": 1, "max": 99},
      "due":      {"type": "date"},
      "status":   {"type": "string", "enum": ["open", "snoozed"]}
    },
    "strict": false
  }
}
```

- Types: `string`, `number`, `integer`, `boolean`, `date` (`YYYY-MM-DD`), `datetime` (ISO 8601), `array`, `object`, `any`. A `null` or missing field counts as missing, so `required` fails for it (and a `patch` that sets a required field to `null` is refused).
- Per field, optionally: `required`, `enum`, `min`, `max` (numbers), `max_length` (strings).
- `strict: true` refuses fields you did not list; by default extra fields are allowed.
- Collections without a schema stay free-form. Names follow the same rules as collections (lowercase, digits, `-`, `_`).
- Set it with `publish --schema-file F`, or replace all of them later with `update REF --schema-file F` (`{}` removes them). Documents already stored are never rewritten: `update` prints the stored ones that no longer match so you can fix them with `patch`.
- Write dates as `YYYY-MM-DD` strings, not words. Keep the schema in step with `--data-notes`.

## Talking to the agent and the app

```js
app.emit('checked-out', { items: 7 })   // tell the agent something happened; it reads these (events)
app.ask('Can you reorder these by aisle?') // opens the chat with this as an UNSENT draft — the user reviews and sends
app.openUrl('https://…')                 // opens the user's browser (http/https only)
app.toast('Saved')
app.artifact  // {id, title}      app.theme  // {dark, accent}      app.uid()  // short unique id
```

Use `app.emit` for "the agent should know" (a form submitted, a decision made) and `app.ask` for "the user wants to say something to the agent". Neither sends anything by itself to the user's chat.

## Look

The style is flat and dense: **hairlines, not boxes**. Text 14px, rows about 32px, 4px radius, no shadows, no gradients, no emoji. Write as little CSS as you can; most pages need none.

**Plain elements are already styled**: `h2` is a small uppercase section label, `table` has a heavy header rule and hairline rows, `input`/`select`/`textarea` are underline-only, `a` and a bare `button` are underlined text links, `input[type=checkbox]` takes the accent. `button.primary` is the single solid button; use at most one per page (Save, Choose).

**Classes** (all optional): `.row` (a flex line with a hairline under it; `.row.top` aligns to the top), `.grow`, `.sub` (second line, small and muted), `.muted`, `.faint`, `.ok`, `.bad` (a status as one coloured word, never a pill), `.done` (struck through and dimmed), `.num` (right-aligned tabular numbers), `.tabs` (underlined text tabs; mark the active one with `aria-current`), `.stats` > `.stat` > `<b>` number + `<span>` label, `label.field` (small label above an input), `.kv` (a label/value pair on one line), `.note` (a callout set off by two hairlines, not a box), `.seg` (a segmented choice: buttons with `aria-pressed`), `.empty`.

**Rules**
- **Structure is optional.** Group only when the data really has groups. A flat list is the default; a section label (`h2`) appears only when it adds meaning.
- **Separate with hairlines**, never cards or filled panels. Emphasis is weight or the accent colour, not a background.
- **Actions are text links**; at most one `button.primary`.
- **Pick the layout that fits the content**, not a list by default: a table for records and comparisons, a time column for schedules, label/value pairs for facts, `.stats` plus a table for trackers, a grid for a week. See `templates/`.
- **Dark mode is automatic** if you only use the variables.

## Building something richer than a list (a canvas, a map, a board)

These pages are fine to build; they just need more care, because you can't see them run.

1. **Data first.** Decide the collections and write them in `--data-notes`. Keep documents small and flat (strings, numbers, ISO dates `YYYY-MM-DD`), reference other documents by id (`task.project = <project id>`), and seed a few clearly-labelled example documents so the page is not empty on first open.
2. **Render from the snapshot, don't rebuild.** `app.subscribe` fires on every change (including the agent's). Keep animation and selection state in variables and merge the new data into it; never recreate the scene, reset a scroll position, or clear a form the user is typing in.
3. **Size from `--app-h`** (see above), then size a `<canvas>` from its box times `devicePixelRatio` (`ctx.setTransform(d,0,0,d,0,0)`). Handle `resize`.
4. **Pointer events only** (`pointerdown/move/up`, `setPointerCapture`), with `touch-action: none` on the canvas, so dragging works on a phone without scrolling the page. Make hit areas generous (the shape plus about 14px). Treat a small movement as a tap, a larger one as a drag.
5. **Use the full area.** Size shapes from the area (`Math.min(width, height)`), not fixed pixels, and leave room for a bottom panel (a hairline-separated list, not a card).
6. **Labels must not collide.** Place a label, test it against the ones already placed (rectangle overlap), and nudge it or drop it. Truncate long text. Text drawn in a canvas cannot use CSS variables: read them once with `getComputedStyle(document.documentElement).getPropertyValue('--text')`, and set the font as a plain string (`'12px system-ui'`).
7. **Colour from data and the theme**, never hard-coded greys; dim by drawing with lower alpha.
8. **Write back small.** A tap that changes something is one `patch` on one document. Anything the agent should notice is `app.emit(name, payload)`; anything the user should say to the agent is `app.ask(text)` (it opens a chat with the text drafted, unsent).
9. **Pause when hidden.** Stop `requestAnimationFrame` while `document.hidden`, and resume on `visibilitychange`.
10. **Check your own page before telling the user it works.** Read it back for the three usual failures: collapsed height (rule above), an exception on empty data (every collection can be `[]`), and an overlay covering the canvas.

`templates/canvas.html` is a working skeleton with all of this wired up.
