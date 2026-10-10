# 0005 — Single-owner setup: URL + sign-in is all the app needs

**Status:** PR 1 (API claim flow) implemented; PRs 2–5 below planned.

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

## Plan
1. **API claim flow** (this PR): `/api/setup`, `/api/setup/claim[/google]`, CLI (`setup-code`,
   `status`, `reset-password`), registration closed, Google non-creating.
2. **Scoped, revocable Hermes token** (replaces the full-power JWT "agent token", which can mint more
   tokens, can't be revoked except by rotating the JWT secret, and expires silently — today's
   expires 2027-08-22) + `connect-hermes.sh` that mints it and writes it into every profile.
3. **Installer**: `install.sh` (secrets, uv, venv, system unit, cloudflared with a pasted tunnel token,
   QR/link output); remove nginx + React frontend; body-size limit + security headers in the API.
4. **Android**: server-address screen, `gaca://setup` deep link, claim and sign-in screens driven by
   `/api/setup`; no baked-in server URL.
5. **Hardening**: login/claim rate limits, refuse the default JWT secret, SECURITY.md.
