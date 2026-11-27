# The page SDK (`window.app`)

Every artifact page gets `window.app` before its own script runs. **A page is a stateless view:** it renders what is in the API and writes changes back through `app`. It has no network (fetch/XHR/WebSocket/iframes are blocked), no cookies or browser storage, and no access to anything but its own data. That is what makes it safe to run a page you just wrote.

## Page rules

- **Self-contained.** One HTML file: inline `<style>` and `<script>`, no CDN scripts, no external fonts, no remote images (the page has no network). For libraries and fonts use the **library shelf** below: one tag and GACA puts the code in for you. Inline SVG, canvas, CSS and `data:` URIs are fine.
- **Mobile first.** It opens full-screen on a phone. No hover-only UI, no horizontal page scrolling (a wide table scrolls inside an `overflow-x:auto` box), no fixed pixel widths. `<meta viewport>` is already set. Rows are compact (about 32px), but **the whole row is the tap target**, not just a small checkbox or icon.
- **Start from the app's look, then design for the content.** The page already ships a stylesheet (see *Look* below): plain elements are styled, and a few classes cover the repeated shapes, so a plain page looks right with no CSS. Go further whenever the content deserves it (see *Look*). Colors come from CSS variables (`--bg`, `--surface`, `--text`, `--muted`, `--faint`, `--accent`, `--on-accent`, `--border`, `--rule`, `--chip`, `--danger`, `--ok`, `--radius`, `--font`, `--mono`); when you add colors of your own, define them as variables with a dark version too (see *Your own colors*), because the page flips to dark mode.
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

## Libraries and fonts (the shelf)

GACA ships a fixed set of pinned libraries and fonts. Ask for one with a single empty tag, **in `<head>`, before your own script**:

```html
<script data-gaca-lib="chart"></script>
<script data-gaca-lib="font-fraunces"></script>
```

When the page opens, GACA replaces the tag with the library's code (or the font), in that spot, so your script below it can use it straight away. Nothing is fetched; it works offline. Only names from this list work; any other name leaves an error in the console naming what is available.

| Name | What it's for | Size |
|---|---|---|
| `preact` | Components and state without a build step: const {h, render} = preact; const {useState, useEffect} = preactHooks; const html = htm.bind(h). | 16 KB |
| `chart` | Line, bar, doughnut, radar charts with tooltips and animation: new Chart(canvas, {type, data, options}). | 203 KB |
| `d3` | Custom SVG charts, scales, shapes, layouts (tree, pack, force), transitions. d3.csvParse/tsvParse are not available (they need eval). | 273 KB |
| `markdown` | Render Markdown text safely: el.innerHTML = DOMPurify.sanitize(marked.parse(text)). | 61 KB |
| `sortable` | Drag to reorder lists and move items between lists, touch friendly: Sortable.create(el, {onEnd}). | 44 KB |
| `confetti` | A celebration burst: confetti({particleCount: 80, spread: 70}). | 24 KB |
| `icons` | 1,600+ line icons: <i data-lucide="calendar"></i> then lucide.createIcons() after each render. | 348 KB |
| `font-inter` | A clean UI sans: font-family: Inter, var(--font). | 47 KB |
| `font-fraunces` | A warm display serif for headings and big numbers: font-family: Fraunces, Georgia, serif. | 36 KB |
| `font-caveat` | A handwritten face for notes and playful labels: font-family: Caveat, cursive. | 48 KB |

- **Use one when it makes the page clearly better**, not by habit: a real chart with tooltips (`chart`), a custom visual (`d3`), drag to reorder (`sortable`), formatted notes (`markdown`), icons (`icons`), a celebration when a goal is hit (`confetti`), a page with a lot of interactive state (`preact`). A list or a table still needs none.
- Each one adds its size to the page every time it opens, so take only what you use (`icons` is the largest).
- Fonts set a family name you then use in CSS: `font-family: Fraunces, Georgia, serif`. Keep body text on `var(--font)`; use a shelf font for headings, big numbers or a deliberate voice.
- `preact` without a build step: `const {h, render} = preact; const {useState, useEffect} = preactHooks; const html = htm.bind(h);` then ``render(html`<${App} />`, document.getElementById('root'))``. Re-render from `app.subscribe` by putting the snapshot in state.
- Colours in charts come from the theme: read them with `getComputedStyle(document.documentElement).getPropertyValue('--accent')` (and your own variables), so dark mode works.
- `icons`: write `<i data-lucide="calendar"></i>` (names from lucide.dev) and call `lucide.createIcons()` after every render.

## Look

**The baseline is the app's "Ledger" look**: flat, calm, hairline dividers, 14px text, compact rows, the slate palette. Plain elements are already styled in it, so a list, a table or a form needs almost no CSS and should usually just use it. Treat it as the default and the guideline, **not a cage**: when a page is something the user will enjoy looking at or use every day (a trip, a habit tracker, a countdown, a dashboard, a game, a family board), design it properly. Make it feel like it was made for this one purpose.

**Plain elements are already styled**: `h2` is a small uppercase section label, `table` has a heavy header rule and hairline rows, `input`/`select`/`textarea` are underline-only, `a` and a bare `button` are underlined text links, `input[type=checkbox]` takes the accent. `button.primary` is the solid button.

**Classes** (all optional): `.row` (a flex line with a hairline under it; `.row.top` aligns to the top), `.grow`, `.sub` (second line, small and muted), `.muted`, `.faint`, `.ok`, `.bad` (a status as one coloured word), `.done` (struck through and dimmed), `.num` (right-aligned tabular numbers), `.tabs` (underlined text tabs; mark the active one with `aria-current`), `.stats` > `.stat` > `<b>` number + `<span>` label, `label.field` (small label above an input), `.kv` (a label/value pair on one line), `.note` (a callout set off by two hairlines), `.seg` (a segmented choice: buttons with `aria-pressed`), `.empty`.

**What you may do** (all of it is fine; use judgement, not all at once):
- **Panels and cards** on `--surface` with `--radius` or a larger radius, when grouping really reads better as blocks (a card per day of a trip, a tile per habit). Soft shadows are fine if subtle.
- **Colour beyond the accent**: a small palette for categories, people or status, a tinted header band, a gentle gradient behind a hero number. Keep text readable (contrast) in light and dark.
- **Visual weight**: big numbers, a hero area at the top (a countdown, today's total, the next event), larger type for what matters most. Still no page title: the app shows it.
- **Drawing**: inline SVG for icons, progress rings, sparklines, charts, simple illustrations; canvas for anything drawn or animated.
- **Motion**: short transitions (150-250ms) when something changes, a check animating in. Respect `@media (prefers-reduced-motion: reduce)`.
- **Emoji** sparingly as icons or category marks where they help scanning (a packing list by person, a meal plan). Not as decoration in every row.
- **More than one button style**: a primary action, secondary outlined or tinted buttons, icon buttons with an `aria-label`.

**What still holds**
- **Readable on a phone first**: generous tap targets (the whole row or tile is the target), no horizontal page scroll, works at 360px wide.
- **Colours through variables**, with a dark version (below). Never a hard-coded colour that only works on white.
- **Content first**: no intro, no "how to use this", no filler. Every element earns its place; decoration supports the content, it does not replace it.
- **Pick the layout that fits the content**, not a list by default: a table for records and comparisons, a time column for schedules, label/value pairs for facts, stats plus a table for trackers, a grid for a week, cards when the items are few and rich. See `templates/`.
- **Consistency inside one page**: one radius, one spacing scale (4/8/12/16/24), one or two type sizes besides body text.

**Your own colors.** Define them once on `:root` and again for dark mode, then use only the variables:

```css
:root { --c-plan: #4E7093; --c-food: #B0923F; --c-fun: #8A6A9E; --tint: color-mix(in srgb, var(--accent) 8%, var(--surface)); }
:root[data-dark] { --c-plan: #8FB0D3; --c-food: #D9BE72; --c-fun: #C0A3D4; }
```

The GACA "crew" colours (slate blue `#4E7093`, gold `#B0923F`, plum `#8A6A9E`, teal `#3F8E93`) are a good starting palette that matches the app. `templates/tracker.html` shows a richer page done this way.

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
