# The cloud link (Parts 8a and 8b)

This server can be linked to one GACA Cloud account. Hermes and the Android/iOS apps never talk to the
cloud: they talk to this server, which forwards with a token it stores for the account. The forwarding
layer is a dumb pass-through: the cloud decides roles, caps and validation.

## Rules

- A gacabit has the same id here and in the cloud. A cloud gacabit is a row in `cloud_pointers` (id, role,
  shared by, cached metadata); the data stays in the cloud.
- The token is stored encrypted (`cloud_link.token_enc`, Fernet) and never leaves the server: not in any
  response, error or log line.
- **Off by default.** `GACA_CLOUD_URL` is empty until you set it (e.g. `https://goodandcheapagent.com/api/v1`). While
  empty, `POST /api/cloud/link/start` answers `409 cloud_not_configured`, nothing is ever called, and the artifact
  list is the local list. The address is copied into the link record when linking starts. It must be https unless
  `GACA_CLOUD_ALLOW_INSECURE_DEV=1`. No request field can change it. Redirects are not followed. The page the app is
  told to open (`verification_uri`) must be on the cloud's own host (or its parent domain), else 502.
- **Paired device only** (the app): link, poll, unlink, preview/redeem an invite, get/install a template, leave a
  shared gacabit, and everything in 8b (below): move to the cloud, bring home, undo, members, invite links,
  validations, publish, and the account's own templates. The person's tap ("Trust and add") is the confirmation; there is no `user_confirmed` flag an agent
  could set for itself.
- **Hermes (API tokens)** gets nothing cloud-related by default: `cloud` is an opt-in scope, not in `hermes connect`
  or `gaca-api token create` defaults. Grant it deliberately: `gaca-api token create --name hermes-x --scopes
  alerts,briefings,artifacts,heartbeat,cloud` (or mint a token with `cloud` in the app's token screen). With it a
  token sees cloud gacabits in `GET /artifacts` (flagged `home: cloud`, `role`), reads/writes their data through
  `/artifacts/{id}/...` (the cloud still decides by role), reads the link status and browses templates. Without it the
  token sees only local artifacts and a cloud id is a plain 404. Even with it: no link/unlink, no invites, no template
  install, no leave.

## Routes

| Route | What |
|---|---|
| `POST /api/cloud/link/start` `{scope?}` | calls the cloud's `/auth/device/start`; returns `{user_code, verification_uri, expires_in, interval}` |
| `POST /api/cloud/link/poll` | one step of `/auth/device/token`: `{status: pending\|linked\|expired\|denied}`; call every `interval` s |
| `GET /api/cloud/link` | `{linked, state, handle, account_id, linked_at, scope}`; never the token |
| `DELETE /api/cloud/link[?force=true]` | refused `409 cloud_artifacts_present` (counts) while any page lives in the cloud, unless `force=true`; then revokes on the cloud (`DELETE /me/server`, falling back to `/me/servers/{id}` on 404/405), then deletes locally; if the cloud is unreachable it still deletes and says `cloud_revoked: false` |
| `POST /api/cloud/invites/preview`, `/redeem` (paired device only) | pass-through; redeem creates the pointer |
| `GET /api/cloud/templates`, `/templates/{id}`, `POST /templates/{id}/get` | pass-through (target `server_bundle` only) |
| `POST /api/cloud/templates/{id}/install` | get the bundle and create the local artifact from it |

`/api/artifacts/{id}/...` for a pointer id is forwarded to `/gacabits/{id}/...`. `GET /api/artifacts` lists local
artifacts and cloud pointers (`home`, `role`, `shared_by`, `member_count`, `stale`). `DELETE` on a pointer means
leave (the owner gets the cloud's `409 owner_cannot_leave`).

## The list when the cloud is down

The cloud listing is cached 15 s. If it cannot be reached, the list answers from the last known pointers with
`stale: true` and does not try again for 45 s (so an outage does not slow every list); only one sync runs at a time;
its connect timeout is 3 s. A partial listing (more than 10 pages) never removes pointers. Long-polls (`wait`) use
their own connection pool so open pages cannot starve writes.

## Errors

The cloud's 403/404/409/410/413/422/423/429 (and others) come back with the cloud's status and body. The cloud
being unreachable, slow, redirecting or answering 5xx is `502 cloud_unavailable`. The cloud rejecting our token
(401) is `502 cloud_link_invalid` (never 401, which would sign the app out) and the link shows `state: invalid`.

## Key and rotation

The key is `GACA_CLOUD_KEY` (comma-separated Fernet keys, newest first) or, if unset, the file `cloud_link.key`
next to the database, created `0600` on first use (back it up separately from the database).

- File key: `gaca-api cloud rotate-key` adds a new key at the top and re-encrypts the stored token. Old keys stay
  in the file; remove them when no old database backup needs them.
- Env key: put the new key first (`GACA_CLOUD_KEY="<new>,<old>"`), restart, run `gaca-api cloud rotate-key`, later
  drop `<old>`.
- Key lost: the token is unreadable (`500 cloud_key_unreadable`). Unlink and link again.

## Part 8b: moving pages between this server and the cloud

Everything in this part is for a **paired device only**. The person's tap in the app is the confirmation; an API token
(Hermes) gets `403` on every route below even with the `cloud` scope, and the Hermes skill has no command for any of
them. Hermes and the apps still never talk to the cloud: this server does, with the stored link. The forwarder stays a
pass-through; the only logic added is the transfer itself.

### Routes

| Route | What |
|---|---|
| `POST /api/artifacts/{id}/move-to-cloud` | local page to cloud page, same id. Blocks until done. `200 {artifact, verified, resumed, backup_expires_at}` |
| `POST /api/artifacts/{id}/bring-home` | cloud page you own to local page, same id. `200 {artifact, confirmed, undo_until, warning}` |
| `POST /api/artifacts/{id}/undo-bring-home` | cancel a bring-home while the cloud still has its copy (7 days) |
| `GET/PATCH/DELETE /api/cloud/gacabits/{id}/members[/{account_id}]`, `POST .../leave` | members |
| `GET/POST /api/cloud/gacabits/{id}/invites`, `DELETE .../invites/{invite_id}` | invite links |
| `GET/POST /api/cloud/gacabits/{id}/validations`, `POST .../publish` | share/publish previews, publish |
| `PATCH/DELETE /api/cloud/templates/{id}`, `POST .../recheck` | the account's own templates (`GET /api/cloud/templates?mine=true` lists them) |

The sharing routes are an explicit allowlist, one declared route per cloud route. A gacabit/template id is validated as 32
lowercase hex (anything else is 422/404 and nothing is sent); body and answer pass through unchanged. `user_confirmed: true` is
set here (never taken from the app) where the cloud wants it from a server token: creating an invite link, publishing,
patching a template, deleting a template (`?user_confirmed=true`), and joining with an invite. An invite link's `url` is returned
to the app once, exactly as the cloud sends it (`Cache-Control: no-store`); this server never stores or logs it.

### Move to the cloud

1. **preflight**: linked (`manage` scope), not archived, id is 32 hex (the cloud only keeps such ids), and the bundle fits the
   cloud's caps (12 MiB inflated, 16 MiB gzip, 1 MB page, 1 MB state, 200 KB per document, 5000 documents per collection,
   25,000 in all, 200 unhandled events, 50 schemas). Too big is `413 too_large_for_cloud` listing each item with its size and
   the limit, **before anything is sent**. `GET /me` proves the cloud is reachable. From here until the end the page is
   read-only (`423 artifact_moving` on any write; reads work). The lock is in memory, so a crash can never leave a page read-only.
2. **uploading**: `POST /uploads`, then one `PUT` of the gzip bundle to the presigned URL (see "The S3 address" below).
3. **importing**: `POST /gacabits/import {upload_id}`. A `503` is retried with the same upload id. If the answer is lost
   (timeout, reset, 5xx) the cloud is asked (`GET /gacabits/{id}`): if the gacabit is there the import worked and we continue.
   `409 conflict` (already in your cloud) is treated the same. `409 id_conflict`, `402`, `413`, `422 validation_blocked` (the
   cloud's findings are in `info.cloud`) and `429` (with `Retry-After`) are passed on.
4. **verifying**: the cloud's copy is read back (page, version, title, schemas, state, state version, every document's data,
   unhandled events) and compared with what was sent. Any difference: nothing changes here (`502 verify_failed`).
5. **converting**: ONE local transaction: the sealed backup row, delete the local rows, create the pointer. The local rows are
   re-read first and must equal what was sent (`409 changed_during_move` otherwise).

The bundle carries meta (title, description, data_notes, crew, archived, created_at, `origin_id` = the page's id), the page, the
4 older page versions plus the current one (the cloud keeps 5), schemas, state, documents with their own
`created_at`/`updated_at`, and the **unhandled** events. Not carried (local or regenerated): user, chat and alert links,
position, last_opened_at, rev, per-document versions (they restart at 1). The cloud stores times to the millisecond.

Errors are always `{code, message, detail, info?, phase?, local_data_changed?}`: `detail` is ALWAYS a string (the same text as
`message`: the Android and iOS apps decode `detail` as a string, like every FastAPI error), and the structured explanation is in
`info` (an object), with `phase` (where it stopped) and `local_data_changed` (`false` for every failed move) lifted to the top
level. A cloud error passed through keeps its own object `detail` under `info` too. A
failed move is remembered (`cloud_status: "move_failed"` in the list/get) and is **always safe to retry**: a cloud copy left by
an earlier try is found, checked against the page as it is now, and used (`resumed: true`), or refused
(`409 cloud_copy_differs`) if the page was edited since.

### Bring home

`POST /gacabits/{id}/bring-home {delete_after: true, user_confirmed: true}` (this puts the cloud copy in `brought_home`:
read-only, members cut off, for 7 days), download the bundle (S3 rules below, size-capped while inflating), validate it against
`bundle.schema.json` and the local limits, restore the page in ONE transaction replacing the pointer (its `rev` is the cloud's
plus one so a polling page always sees it as news), and only then `POST .../bring-home/confirm`. The backup, if one still
exists, is a fallback only: it supplies crew, chat/alert links and last-opened (a bundle cannot carry them) and the time/note of
the current page version when that page is unchanged; nothing the cloud sent is overridden.

If anything fails before the local restore, the cloud's bring-home is undone, the pointer stays, and the error says so
(`info.cloud_state: "restored"`; `"brought_home"` plus the undo deadline if even the undo failed, in which case the next try
undoes and redoes it). If only the confirm fails the page IS home (`confirmed: false`, `cloud_status:
"bring_home_unconfirmed"`): the cloud keeps its copy until it hears, and the confirm is repeated on the next call and by the
housekeeping loop. `undo-bring-home` on a page that came home restores the cloud copy and turns the page back into a cloud
page, but only if it was not edited here since (`409 local_changed_since_bring_home`; nothing is overwritten).

### The S3 address (the one request that is not to the cloud)

The presigned URL is data from the network, so it is checked before any connection: `https`, host an AWS S3 endpoint
(`<bucket>.s3[.<region>|-<region>].amazonaws.com` or path style `s3[.<region>].amazonaws.com`), no userinfo, no backslash,
whitespace or non-ASCII, port 443, and the host the HTTP client will really use is re-checked. Anything else is `502
transfer_url_rejected` and no connection is made. No redirects, TLS verified, environment proxies ignored, no Authorization or
request-id header (only the `content-type`, `content-encoding` and `x-amz-*` headers the presign names), size caps both ways,
timeouts, and the URL (its query is a signature) is never in an error or a log. Any S3 bucket matches the host rule; the cloud
is trusted to name its own bucket.

### What a move does not carry, and the acknowledgement

`GET /api/artifacts/{id}/move-preview` (paired device) says, without sending anything: whether it fits (sizes and caps), and
`not_carried`: `versions_dropped` (page versions beyond the 5 the cloud keeps), `handled_events_dropped`, `crew`,
`chat_alert_links`, `doc_versions_reset` (documents restart at version 1), `last_opened`. If any of it is non-empty,
`move-to-cloud` needs `{"acknowledge_not_carried": true}` in its body, else `409 confirmation_required` with the same summary
in `info.not_carried` and nothing is sent. The result repeats `not_carried`. `member_impact` is `none`: a page that is not in
the cloud has no members. Numbers the cloud cannot store (whole numbers beyond 64 bits, NaN, Infinity) are refused first with
`422 unsupported_number` and the JSON paths in `info.paths`.

After a bring-home the cloud keeps its copy (status `brought_home`) for 7 days, so the same page cannot be moved to the cloud
again until the cloud has purged it or the bring-home is undone (`409 cloud_copy_not_usable`).

Bringing home a page other people share cuts every member off, so when the cloud reports more than one member it needs
`{"acknowledge_members": true}` (else `409 members_will_lose_access`, `info.member_count`); the result says how many lost access.

### Verification

After the import every part of the cloud's copy is read back and compared TYPE-STRICT (canonical JSON, so `true` is not `1`,
`1.0` is not `1`, NFC is not NFD): title, description, data notes, archived, schemas, page and page version, state and its
version, every document's data and created/updated times (to the millisecond), the older page versions (their html, note and
time) and the unhandled events with their times. Any difference refuses the move.

### Responsiveness

The CPU-heavy parts (building, compressing and validating the bundle, the comparison, the backup's gzip, parsing a download) run
in worker threads; only the SQLite swap itself runs on the connection thread, as one transaction. Every transfer has a total
wall-clock limit (`GACA_CLOUD_TRANSFER_TOTAL_SECONDS`, 120 s) as well as per-read timeouts.

### Fields

`GET /api/artifacts` and `GET /api/artifacts/{id}` carry `home` (`local` or `cloud`), `moving` (true while a move runs) and
`cloud_status`: for a cloud page the cloud's status (`active`, `read_only`, `brought_home`...); for a local page null, or
`moving`, `move_failed`, `bring_home_unconfirmed`.

### The sealed backup

When a page becomes a cloud page, its complete local content (page, every kept version, state, documents, all events, handled
included) is written to `artifact_cloud_backups` (gzip JSON + sha256) **in the same transaction** that deletes the local rows,
`expires_at` = 30 days (`GACA_CLOUD_BACKUP_DAYS`). A housekeeping loop (first run a minute after start, then hourly) deletes a
backup only when it is past that AND it can no longer be needed: the page is local again, or the cloud copy was just checked
(`GET /gacabits/{id}` is 200 with status `active`; checked at most daily per backup). The newest backup of a page that still has a
pointer is kept at least 90 days. A copy that is missing, not active or unreachable keeps the backup, with a warning in the log,
checked again the next day. Nothing is kept past one year. The loop also repeats unsent bring-home confirms, and never while a
transfer of that page is running.

- `gaca-api cloud list-backups`: id, page, days left (or "past its 30 days; kept until the cloud copy is verified healthy"), size.
- `gaca-api cloud restore-backup <backup id or page id>`: puts the page back exactly as it was (event numbers and document
  versions included), removes the pointer, refuses if a local page with that id exists. **The cloud is not touched**: a copy
  there may hold newer changes, which are not in the backup. Check it before deleting it.

### Failure and recovery

| What went wrong | What the app sees | Local data | Cloud | What to do |
|---|---|---|---|---|
| too big, archived, id not portable, not linked, link read-only | 413 `too_large_for_cloud` (items listed) / 409 / 422 / 403, nothing sent | unchanged | untouched | trim, unarchive, link again |
| cloud unreachable or 5xx before the upload | 502 `cloud_unavailable`, phase | unchanged | untouched | retry |
| `POST /uploads` refused (402/413/429/503) | the cloud's status and code, phase `uploading` | unchanged | untouched | as the code says; 429 has `Retry-After` |
| S3 refuses or drops the PUT (network/5xx retried twice) | 502 `transfer_failed` | unchanged | an unused upload expires in 15 min | retry |
| upload address not S3, or odd headers | 502 `transfer_url_rejected` | unchanged | untouched | retry; report if it persists |
| import 402 / 413 / 429 | passed on (`billing_required`, `too_large_for_cloud`, `rate_limited`) | unchanged | nothing imported | set up billing / trim / wait |
| import 409 `id_conflict` | 409 `id_conflict` | unchanged | another account holds the id | contact support: the id cannot change |
| import 422 `validation_blocked` | 422 with `info.cloud.findings` | unchanged | nothing imported | fix what the findings name |
| import 503 / answer lost | retried with the same upload; if the gacabit is there, continues | unchanged | imported once | none; if it still fails, retry the move |
| verify mismatch | 502 `verify_failed`, `info.mismatches` | unchanged | a copy exists (not deleted) | retry (re-verified); report if it persists |
| a copy from an earlier try differs from the page now | 409 `cloud_copy_differs` | unchanged | the stale copy stays | remove it on the website or bring it home, then retry |
| page edited during the move | 409 `changed_during_move` | the edit is kept | a copy exists | retry |
| crash or disk error while converting | 500 `move_failed` (transaction rolled back) | unchanged, no backup row | a verified copy exists | retry: continues without uploading |
| cloud lost or damaged a page after a successful move | n/a | pointer only | the problem | within 30 days: `gaca-api cloud restore-backup` |
| bring home: download or validation fails | 502 `bundle_invalid` / `transfer_failed` / `transfer_url_rejected`, `info.cloud_state` | pointer stays | undone (`restored`) | retry |
| bring home: that undo also fails | same, `cloud_state: "brought_home"`, `undo_until` | pointer stays | locked read-only, not deleted | retry (undoes and redoes) or undo on the website |
| bring home: confirm fails | 200 `confirmed: false` | page is home | keeps its copy until told | repeat the call; housekeeping repeats it |
| unlink with pages in the cloud, an unconfirmed bring-home, or a transfer running | 409 `cloud_artifacts_present` (`info`: owned, shared, unconfirmed_bring_homes, in_progress) | unchanged | untouched | finish / bring them home, or `?force=true` |
| `force=true` | pointers deleted (the pages stay in the cloud but this server and the app can no longer open them until it is linked again, which brings the pointers back); sealed backups kept; an unconfirmed bring-home stays unconfirmed so the cloud keeps its copy | | | |
