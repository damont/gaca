# The page SDK (`window.app`)

Every artifact page gets `window.app` before its own script runs. **A page is a stateless view:** it renders what is in the API and writes changes back through `app`. It has no network (fetch/XHR/WebSocket/iframes are blocked), no cookies or browser storage, and no access to anything but its own data. That is what makes it safe to run a page you just wrote.

## Page rules

- **Self-contained.** One HTML file: inline `<style>` and `<script>`, no CDN scripts, no external fonts, no remote images. Inline SVG, canvas, CSS and `data:` URIs are fine. Charts: draw with SVG/canvas yourself.
- **Mobile first.** It opens full-screen on a phone. Big tap targets (≥ 44px), 16px+ text, no hover-only UI, no horizontal scrolling, no fixed pixel widths. `<meta viewport>` is already set.
- **Use the app's look.** Colors and fonts come as CSS variables: `--bg` (page), `--surface` (cards), `--text`, `--muted`, `--accent`, `--on-accent` (text on accent), `--border`, `--chip`, `--danger`, `--ok`, `--radius`, `--font`. Use them instead of hard-coded colors so the page matches the app. `body` already has padding and the base font.
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

## Talking to the agent and the app

```js
app.emit('checked-out', { items: 7 })   // tell the agent something happened; it reads these (events)
app.ask('Can you reorder these by aisle?') // opens the chat with this as an UNSENT draft — the user reviews and sends
app.openUrl('https://…')                 // opens the user's browser (http/https only)
app.toast('Saved')
app.artifact  // {id, title}      app.theme  // {dark, accent}      app.uid()  // short unique id
```

Use `app.emit` for "the agent should know" (a form submitted, a decision made) and `app.ask` for "the user wants to say something to the agent". Neither sends anything by itself to the user's chat.
