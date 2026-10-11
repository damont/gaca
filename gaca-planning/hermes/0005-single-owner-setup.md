# 0005 — Single-owner setup: URL + sign-in is all the app needs

**Status:** claim flow, scoped tokens, API hardening and the installer are implemented; removing nginx/React, the Android flow, and SECURITY.md are planned.

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

**No per-profile step.** A crew member created in the app gets its scoped token in the same write as
its other settings; a profile made by hand with `hermes profile create` gets one the next time the app
discovers it (best-effort: wiring never fails discovery, and a token whose write failed is revoked).
`gaca-api hermes connect` is for first-time setup/migration and the `default` profile (whose `.env`
the sandboxed service may not write); it is safe to re-run (only wires profiles that lack a token;
`--rotate` replaces them).

Also fixed here: the Swagger UI was mounted at `/api/agent`, shadowing the real `GET /api/agent`
status route (the app got an unauthenticated HTML page instead of JSON). Docs are now at `/api/docs`.

## Plan
1. **API claim flow** (this PR): `/api/setup`, `/api/setup/claim[/google]`, CLI (`setup-code`,
   `status`, `reset-password`), registration closed, Google non-creating.
2. **Scoped, revocable Hermes token** (this PR) + `gaca-api hermes connect`. Migrating the live box,
   then rotating the JWT secret (retiring the old agent JWTs, which expire silently 2027-08-22),
   are deploy steps.
3. **API hardening for life without nginx** (PR 3a): security headers, 3 MiB request cap, per-client
   rate limits on sign-in/claim (real IP from `CF-Connecting-IP`, trusted only from a loopback peer),
   CORS off by default (`GACA_CORS_ORIGINS`), refusing to start with the placeholder/short JWT secret
   (since removed with the sign-in). **Installer** (3b, done): `./install.sh` (uv, deps, a strong `.env` that never clobbers an
   existing secret, the service, cloudflared with a pasted tunnel token or a temporary quick tunnel, the setup
   code as a QR/`gaca://setup` link, then waits for the claim and wires Hermes). `--service system` is the sandboxed
   default and needs root; without a way to get root it leaves ONE script for the operator and a re-run
   resumes. `--service user` needs no root and has no sandbox (said plainly). `./install.sh uninstall [--purge]`.
   Tested for real on an arm64 box (user mode, quick tunnel): claim, rate limit, body cap, headers, idempotent re-run, Hermes wiring, uninstall. **Then** remove
   nginx + React frontend from the repo and production (a coordinated cutover).
4. **Android + pairing** (replaces the claim/sign-in screens): no accounts. `gaca-api pair` on the box prints a
   one-time code and a QR that also carries the server address (`GACA_PUBLIC_URL`); the app scans it (or opens the
   `gaca://setup` link, or takes a typed address + code) and receives a revocable device credential. Many devices
   per server, one server per phone; `gaca-api device list|revoke`. No baked-in server URL. The old
   password/Google endpoints are removed in a follow-up once a device is paired.
5. **Hardening**: login/claim rate limits, refuse the default JWT secret, SECURITY.md.


## Status, 2026-10-11
Built and deployed: hardening (#27), installer (#28), device pairing (#29), sign-in removal (#30; stored password
hashes and Google ids scrubbed from existing databases at startup). nginx, the React frontend, the Dockerfile and the
compose files are removed from the repo; the API listens on loopback TCP only (the Unix socket that fed the frontend's
nginx is gone). Production cutover on david-claw: cloudflared on the host network pointing at `http://localhost:41700`
(Cloudflare dashboard: the public hostname's service changes from `http://nginx:80`). Still to do: SECURITY.md, generic
(de-personalised) skills, APK release, licence, and the split of private planning material into its own repository.

### Hermes is a requirement (2026-10-11)
The installer checks, before it changes anything, that Hermes is on the machine and ready: the `hermes` command at
0.21 or newer, a Hermes home (`hermes setup` makes it), the gateway API server switched on (`API_SERVER_ENABLED`,
`API_SERVER_KEY`), and the gateway answering. Anything missing stops the install with the exact fix (exit 4; a
re-run resumes). GACA does not install or configure Hermes itself; `--install-hermes` only runs Hermes's own
installer, and `hermes setup` (model credentials) is always the person's. `--no-hermes` skips the checks for a
server-only try-out.

GACA's Hermes skills live in the repo's `skills/` (`gaca`, `gaca-alerts`, `gaca-briefing`, `gaca-lists`,
`gaca-artifacts`) and are copied into every profile's `skills/gaca/` (provisioning/skill_sync.py): by the installer
and `gaca-api hermes connect` for all profiles including default, and by the service for a crew member created in
the app or discovered (named profiles only; `default` is outside the sandbox's write set). A manifest records what
was written, so a file an agent has edited since is left alone and reported, and an unmodified one follows upstream.
