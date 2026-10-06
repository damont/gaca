# Artifacts — API contract

An **artifact** is an HTML page an agent (Hermes) writes, plus the data it keeps. The page is a stateless
view; its data lives in the API, so the page can be rewritten without losing anything and the agent can
read and write the same data while the page is closed. The app opens a page in a locked-down WebView and
gives it `window.app` (see `hermes-skill/gaca-artifacts/references/sdk.md`). Authoritative schema:
`/api/openapi.json`. Everything is scoped to the caller; someone else's id is a 404.

## Pages
| | |
|---|---|
| `POST /api/artifacts` | `{title, html, description?, data_notes?, crew?, session_id?, alert_id?, state?, collections?}` → `ArtifactOut` (201). `collections` seeds `{name: {docId: {...}}}`. `html` ≤ 1 MB. |
| `GET /api/artifacts` | `?include_archived&session_id&alert_id&q&limit` → `{artifacts: [...]}` in the owner's own order (`position`); pages never placed yet come first, newest first |
| `PUT /api/artifacts/order` | `{ids: [...]}` → the owner's drag-and-drop order: listed ids first, in that order, the rest keep their relative order after them (archived pages included). 404 on an unknown / not-yours id. Returns the non-archived list |
| `GET /api/artifacts/{id}` | metadata incl. `version`, `rev`, `state_version`, `link` (`gaca://artifact/<id>`) |
| `GET /api/artifacts/{id}/html` | the page, `text/html` (`?version=N` for an old one) |
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

## Events (page → agent)
`POST …/events {name, payload}` (what `app.emit` calls), `GET …/events?after&unhandled&limit`, `POST …/events/handled {upto}`.

## Where artifacts show up
- **Artifacts tab** in the app (all pages, search, archive, delete).
- **Chat:** the crew member includes `gaca://artifact/<id>` (or `[Label](gaca://artifact/<id>)`) in its reply; the app renders a tappable chip and hides the raw address.
- **Alerts:** `POST /api/alerts` accepts `artifact_id` (404 if not the caller's); `AlertOut.artifact_id`; the alert detail shows an "Open page" chip, the inbox row a small page icon.
- Deep link: `gaca://artifact/<id>` opens the page from outside the app.

## Page sandbox (Android)
CSP `default-src 'none'` + inline script/style + `data:`/`blob:` images and fonts (no network of any kind),
WebView with file/content access, DOM storage, geolocation and multiple windows off, all navigation blocked
(a tapped http(s) link opens in the browser), token never exposed: the page reaches the API only through a
bridge that talks to this artifact's own routes.
