# GACA

A phone app for the AI agent you run yourself. GACA is a small server plus an Android app: your
[Hermes](https://github.com/NousResearch/hermes-agent) agent posts alerts, briefings, lists and
pages to the app, and you chat with it from anywhere.

**One server belongs to one person.** You run it on a machine you control (next to Hermes), pair
your phone with it, and that is the whole account system: no sign-up, no passwords, no other users.

```
phone app ──https──▶ Cloudflare Tunnel ──▶ gaca-api (127.0.0.1:41700) ◀── Hermes (same machine)
```

## What you need

- **Hermes, installed and set up on the same machine.** This is required: GACA is the phone app for a
  Hermes agent, and without one the app has nothing to talk to. GACA does not install or configure Hermes
  for you (it has its own installer, upgrades and your model credentials). It checks that Hermes is ready
  and tells you exactly what is missing. See [Set up Hermes](#set-up-hermes-once) below.
- A Linux machine with systemd.
- Docker, only to run `cloudflared` (the tunnel). Not needed if you skip the tunnel.
- For a permanent address: a free Cloudflare account and a domain on it. To just try it, a temporary
  `trycloudflare.com` address needs no account.
- An Android phone (an iPhone app is in progress).

## Set up Hermes (once)

1. Install it with Hermes's own installer, then choose your model provider:
   ```bash
   curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
   hermes setup
   ```
   You need Hermes **0.21 or newer**.
2. Turn on Hermes's API server, which is how GACA talks to your agent. Add to `~/.hermes/.env`:
   ```
   API_SERVER_ENABLED=true
   API_SERVER_KEY=<any long random string>
   ```
   (`python3 -c "import secrets; print(secrets.token_urlsafe(32))"` makes one.)
3. Run the gateway as a service and check that it answers:
   ```bash
   hermes gateway install
   curl http://127.0.0.1:8642/health
   ```

The GACA installer checks all of this before it changes anything, and stops with the exact fix for whatever
is missing. `./install.sh --install-hermes` will run Hermes's installer for you if it is not there (you still
run `hermes setup` yourself, because that is where your model credentials go).

## Install

```bash
git clone <this repo> gaca && cd gaca
./install.sh
```

The installer is safe to re-run (it keeps your data and settings):

- checks Hermes (above) and puts GACA's skills into it (see [Hermes](#hermes));
- installs `uv` and the dependencies, writes `.env`, and starts `gaca-api` as a systemd service
  (sandboxed system service by default, which needs `sudo`; `--service user` needs no root and has no sandbox);
- brings up the tunnel: paste a Cloudflare tunnel token for a permanent address, or choose a temporary one;
- prints a **QR code**, waits for you to pair your phone, then gives Hermes its token for GACA.

Useful options: `--tunnel token|quick|none`, `--hostname gaca.example.com`, `--service system|user`,
`--install-hermes`, `--no-hermes` (set up only the server, e.g. to try it without an agent). `./install.sh --help`
lists them all.

For a permanent address, in the Cloudflare dashboard (Zero Trust → Networks → Tunnels) create a tunnel,
copy its token, and add a published application route (public hostname) whose service is **HTTP `localhost:41700`**.

## Pair your phone

Install the app, open it, tap **Scan QR code**, and scan the code `gaca-api pair` shows. Run it on the
server any time you want to add a phone:

```bash
cd gaca/gaca-api && uv run gaca-api pair        # a one-time code + QR, valid 10 minutes
uv run gaca-api device list                     # paired phones
uv run gaca-api device revoke <id>              # unpair one
```

A phone talks to one server at a time (**Settings → Disconnect from this server** to switch). The QR also
works as a link, `gaca://setup?url=…&code=…`, and "Enter a code instead" takes the address and code by hand.

## Hermes

Two things connect Hermes to GACA, both done by the installer and redone by `uv run gaca-api hermes connect`:

- **A token per profile.** Each Hermes profile gets its own scoped, revocable token (alerts, lists, briefings,
  artifacts only) in its `.env`. Hermes talks to GACA over `127.0.0.1`, never the public address.
- **Skills.** GACA's skills (the repo's `skills/`) are copied into every profile's `skills/gaca/` folder so the
  agent knows how to use GACA:

  | skill | teaches the agent to |
  |---|---|
  | `gaca` | reach GACA safely (local address, token, live API schema) and pick the right skill |
  | `gaca-alerts` | send alerts and permission requests to your phone instead of only the chat |
  | `gaca-briefing` | build the daily briefing in sections plus a summary, and schedule it |
  | `gaca-lists` | keep shopping, gift and to-do lists in GACA, not in memory |
  | `gaca-artifacts` | publish interactive pages (trackers, planners) that open in the app |

Profiles you create later, in the app or by hand, get both automatically. Re-running `./install.sh` (or
`gaca-api hermes connect`) after a `git pull` updates the skills; a skill file that an agent has edited is left
alone and reported, never overwritten. `gaca-api token list|revoke` manages the tokens.

## Update and uninstall

```bash
git pull && ./install.sh              # update
./install.sh uninstall                # remove the service and tunnel, keep your data
./install.sh uninstall --purge        # ...and delete the data and settings
```

## Security model

- The server only listens on loopback; the tunnel is the only way in.
- The one public endpoint that matters is pairing: single-use codes that expire in minutes, are stored
  hashed, are burned after repeated wrong guesses, and are rate-limited per client.
- Phones and Hermes hold individually revocable credentials, stored hashed on the server. A Hermes token
  can only post and read what its scopes name; it cannot chat as you, manage devices, or mint tokens.
- Request size caps, security headers and rate limits are built into the API (there is no nginx).

## Development

```bash
cd gaca-api
uv sync
uv run pytest
GACA_AGENT_ENGINE=hermes GACA_DB_PATH=./dev.db uv run gaca-api     # serves on 127.0.0.1:41700
```

Design notes live in `gaca-planning/`.
