# The cloud link (Part 8a)

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
  shared gacabit. The person's tap ("Trust and add") is the confirmation; there is no `user_confirmed` flag an agent
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
| `DELETE /api/cloud/link` | revokes on the cloud (`/me/servers/{id}`), then deletes locally; if the cloud is unreachable it still deletes and says `cloud_revoked: false` |
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
