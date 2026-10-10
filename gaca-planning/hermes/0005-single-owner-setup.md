# 0005 — Single-owner setup: URL + sign-in is all the app needs

**Status:** PR 1 (API claim flow) and PR 2 (scoped tokens) implemented; PRs 3–5 below planned.

## Decisions (David, 2026-10-10)
- **One owner per server.** Everyone hosts their own instance. No invites, no roles, no member accounts.
  (Inviting someone would hand them an agent that can run commands on the host; we don't offer it.)
- **No nginx, no React frontend.** The app talks straight to the API through the Cloudflare Tunnel.
  Size limit and security headers move into the API. Docker is only for `cloudflared` (optional).
- **Cloudflare setup defaults to pasting a tunnel token.** Automating tunnel+DNS creation with a
  Cloudflare API token is an optional extra, not the default.

## The flow
```
operator, once:   install.sh  ->  API + tunnel + Hermes connection  ->  prints QR + gaca://setup link
phone, first time: open link (or enter URL) -> "Set up this server" -> email+password (or Google) -> in
phone, every time after (any device): enter URL -> sign in -> in
```
The app learns what to show from `GET /api/setup` (public): `claimed`, `sign_in` methods,
`google_client_id`. No code, key, or token is ever typed after the first claim.

## Why the first claim needs a code
A fresh public URL belongs to whoever reaches it first. The one-time **setup code** is shown only on
the server's console, so only the operator has it. Stored hashed, 60-minute expiry, single use,
burned after 10 wrong guesses, replaced by `gaca-api setup-code`. Once an owner exists the claim
endpoints answer 409 forever.

## Security properties (PR 1)
- Both doors that used to create accounts are shut: `POST /auth/register` always 403, and Google
  sign-in **never creates** an account (it used to for any verified Google email). A Google identity
  that isn't the owner's gets 403.
- Claim is serialized (a lock), so two simultaneous claims make exactly one owner.
- Lost password: `gaca-api reset-password` on the box (shell access = proof of ownership).
- The CLI refuses to create a database, so a wrong `GACA_DB_PATH` can't silently produce a second,
  empty "server" with its own setup code.

## Google sign-in is optional, by design
The server checks Google tokens against its own `GACA_GOOGLE_CLIENT_ID`; a public APK can't assume the
operator set one up (and needs the app's package + SHA-1 registered in their Google project). So
email+password is the default and Google is an advanced option the server advertises via `/api/setup`.

## API tokens (PR 2)
Hermes skills authenticate with an **API token** (`gaca_pat_…`), not a sign-in session:
- stored as a SHA-256 hash only; the secret is shown once at creation and never printed by
  `hermes connect`;
- **scoped**: `alerts`, `lists`, `briefings`, `artifacts`. A token reaches only those endpoints (on
  alerts: create and read — not act/dismiss/snooze/delegate/take-to-chat, which are the owner's
  decisions). Chat, crew, the agent connection, push, account and token management answer 403;
  dot segments / empty path segments fail closed;
- **revocable** individually (`gaca-api token revoke`, `DELETE /api/tokens/{id}`), takes effect at
  once; **no expiry unless asked for**, so nothing silently dies in a year; `last_used_at` shows dead ones;
- **cannot manage tokens**: minting/revoking needs a real sign-in session, enforced twice (the
  scopes don't include it, and the endpoints demand a session);
- `POST /auth/agent-token` is **removed** (it minted a full-power, unrevocable JWT that could mint more).
  Existing agent JWTs keep working until the JWT secret is rotated.
- `gaca-api hermes connect` mints one token per Hermes profile (`hermes-<profile>`), writes
  `GACA_BASE_URL` + `GACA_API_TOKEN` into that profile's `.env` (atomic, 0600, symlinks replaced not
  followed, other lines untouched), and revokes the profile's previous token. Write first, revoke
  after; a failed write revokes the new token rather than orphaning it.

Also fixed here: the Swagger UI was mounted at `/api/agent`, shadowing the real `GET /api/agent`
status route (the app got an unauthenticated HTML page instead of JSON). Docs are now at `/api/docs`.

## Plan
1. **API claim flow** (this PR): `/api/setup`, `/api/setup/claim[/google]`, CLI (`setup-code`,
   `status`, `reset-password`), registration closed, Google non-creating.
2. **Scoped, revocable Hermes token** (this PR) + `gaca-api hermes connect`. Migrating the live box,
   then rotating the JWT secret (retiring the old agent JWTs, which expire silently 2027-08-22),
   are deploy steps.
3. **Installer**: `install.sh` (secrets, uv, venv, system unit, cloudflared with a pasted tunnel token,
   QR/link output); remove nginx + React frontend; body-size limit + security headers in the API.
4. **Android**: server-address screen, `gaca://setup` deep link, claim and sign-in screens driven by
   `/api/setup`; no baked-in server URL.
5. **Hardening**: login/claim rate limits, refuse the default JWT secret, SECURITY.md.
