# Artifacts — API contract

An **artifact** is an HTML page an agent (Hermes) writes, plus the data it keeps. The page is a stateless
view; its data lives in the API, so the page can be rewritten without losing anything and the agent can
read and write the same data while the page is closed. The app opens a page in a locked-down web view and
gives it `window.app` (see `skills/gaca-artifacts/references/sdk.md`). **The API builds the page and
performs its calls** (`/page`, `/call` below) — a client only loads HTML and relays messages, so Android,
iPhone and web behave identically. Authoritative schema:
`/api/openapi.json`. Everything is scoped to the caller; someone else's id is a 404.

## Pages
| | |
|---|---|
| `POST /api/artifacts` | `{title, html, description?, data_notes?, crew?, session_id?, alert_id?, state?, collections?}` → `ArtifactOut` (201). `collections` seeds `{name: {docId: {...}}}`. `html` ≤ 1 MB. |
| `GET /api/artifacts` | `?include_archived&session_id&alert_id&q&limit` → `{artifacts: [...]}` in the owner's own order (`position`); pages never placed yet come first, newest first |
| `PUT /api/artifacts/order` | `{ids: [...]}` → the owner's drag-and-drop order: listed ids first, in that order, the rest keep their relative order after them (archived pages included). 404 on an unknown / not-yours id. Returns the non-archived list |
| `GET /api/artifacts/{id}` | metadata incl. `version`, `rev`, `state_version`, `link` (`gaca://artifact/<id>`) |
| `GET /api/artifacts/{id}/html` | the page as the agent wrote it, `text/html` (`?version=N` for an old one) |
| `GET /api/artifacts/{id}/page` | the page **ready to show**: the current HTML with the app prelude inserted right after `<head>` (or a `<head>` added after `<html>`, or a whole document around a bare fragment) — CSP meta, viewport meta, theme CSS variables, `window.__GACA_INIT = {artifact:{id,title}, theme:{dark:false, accent:"#2D3C4C"}}`, then the SDK. `text/html`, `Cache-Control: no-store`, CSP also sent as a header. Load this in the web view; does not mark the page opened (`/opened` still does) |
| `POST /api/artifacts/{id}/call` | `{method, args}` — one `window.app.*` data call relayed from the page; see "Page calls" |
| `PATCH /api/artifacts/{id}` | `{title?, description?, data_notes?, archived?, html?, note?}`; new `html` makes a new version (last 20 kept); data untouched |
| `GET /api/artifacts/{id}/versions`, `POST …/restore {version}` | history; restore creates a new version |
| `POST /api/artifacts/{id}/opened` | app marks "recently used" (does not change `updated_at`) |
| `DELETE /api/artifacts/{id}` | deletes the page and all its data |

## Data
Two shapes, use either or both. Any data write bumps `rev`, so a page can poll cheaply.

- `GET /api/artifacts/{id}/data[?since_rev=N]` → `{rev, changed, state, state_version, collections}`; with `since_rev == rev` → `{rev, changed:false}` only.
- **State** (one JSON object): `GET/PUT/PATCH …/state`. `PUT {state, expected_version?}`; `PATCH {patch, expected_version?}` is an RFC 7386 merge patch. Stale `expected_version` → `409`. ≤ 1 MB.
- **Collections** (keyed documents, one row each — two writers adding at once never collide): `GET/POST …/collections/{name}`, `GET/PUT/PATCH/DELETE …/collections/{name}/{id}`, `DELETE …/collections/{name}` (clear). Names `[a-z0-9][a-z0-9_-]{0,63}`, ids `[A-Za-z0-9][A-Za-z0-9_.-]{0,63}`. Documents ≤ 200 KB, ≤ 5000 per collection. `expected_version` → `409` on mismatch; `POST` with a taken id → `409`.
- `POST …/batch {ops:[{op: set|patch|delete|clear, collection, id, data}]}` — all or nothing (≤ 200 ops).

## Page calls — `POST /api/artifacts/{id}/call`
The SDK calls `window.GacaBridge.call(id, method, argsJson)` on Android (unchanged), else
`window.webkit.messageHandlers.gaca.postMessage({id, method, args})` on iOS, else rejects with
`Error('no app bridge')`. The client relays `{method, args}` here and answers the page with
`window.__gaca.resolve(id, ok, json)` (`json` a string): on 2xx `ok=true` and the response body; otherwise
`ok=false` and `{"status": <http status>, "message": <detail>}`.

| `method` | same as | args used (others ignored, nulls dropped) |
|---|---|---|
| `data` | `GET …/data` | `since_rev` |
| `state.get` / `state.set` / `state.patch` | `GET` / `PUT` / `PATCH …/state` | `state`, `expected_version` / `patch`, `expected_version` |
| `doc.list` / `doc.clear` | `GET` / `DELETE …/collections/{collection}` | `collection` |
| `doc.add` | `POST …/collections/{collection}` | `collection`, `data`, `id` |
| `doc.set` / `doc.patch` / `doc.remove` | `PUT` / `PATCH` / `DELETE …/collections/{collection}/{id}` | `collection`, `id`, + `data`/`patch`, `expected_version` |
| `batch` | `POST …/batch` | `ops` |
| `emit` | `POST …/events` | `name`, `payload` |

The response is exactly what the "same as" endpoint returns — same JSON, same error status and `detail`
(`200` with `null` where that endpoint answers `204`). A bad `collection` / `id` is `422`
`bad collection name` / `bad document id`. Any other method is `400 unknown method <m>` — including the UI
calls `ask`, `openUrl` and `toast`, which act on the app's own screen and are handled by the client
(`ask`: open the chat with the text, ≤ 4000 chars, as an unsent draft; `openUrl`: http(s) only, else reject
with status 400; `toast`: ≤ 200 chars).

## Events (page → agent)
`POST …/events {name, payload}` (what `app.emit` calls), `GET …/events?after&unhandled&limit`, `POST …/events/handled {upto}`.

## Where artifacts show up
- **Artifacts tab** in the app (all pages, search, archive, delete).
- **Chat:** the crew member includes `gaca://artifact/<id>` (or `[Label](gaca://artifact/<id>)`) in its reply; the API returns the message's `display_content` with the address removed and the linked pages as `artifacts: [{id, title, description, crew, missing}]` chips (see alerts/api-contract.md §6).
- **Alerts:** `POST /api/alerts` accepts `artifact_id` (404 if not the caller's); `AlertOut.artifact_id`, plus `AlertOut.artifact` — the same chip shape, resolved (`missing: true`, title `"Page"`, if it has since been deleted); the alert detail shows an "Open page" chip, the inbox row a small page icon.
- Deep link: `gaca://artifact/<id>` opens the page from outside the app.

## Page sandbox (all clients)
CSP `default-src 'none'` + inline script/style + `data:`/`blob:` images and fonts (no network of any kind) —
set by `/page` itself. The client's web view should have file/content access, DOM storage, geolocation and
multiple windows off and block all navigation (a tapped http(s) link opens in the browser). The token never
reaches the page: the client fetches `/page` and relays `/call` with its own credentials, and `/call` can only
touch this artifact's own data.
