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

- A Linux machine with systemd. Hermes lives here too if you want an agent behind it.
- Docker, only to run `cloudflared` (the tunnel). Not needed if you skip the tunnel.
- For a permanent address: a free Cloudflare account and a domain on it. To just try it, a temporary
  `trycloudflare.com` address needs no account.
- An Android phone.

## Install

```bash
git clone <this repo> gaca && cd gaca
./install.sh
```

The installer sets everything up and is safe to re-run (it keeps your data and settings):

- installs `uv` and the dependencies, writes `.env`, and starts `gaca-api` as a systemd service
  (sandboxed system service by default, which needs `sudo`; `--service user` needs no root and has no sandbox);
- brings up the tunnel: paste a Cloudflare tunnel token for a permanent address, or choose a temporary one;
- prints a **QR code**, waits for you to pair your phone, then wires Hermes to the server.

Useful options: `--tunnel token|quick|none`, `--hostname gaca.example.com`, `--service system|user`,
`--no-hermes`. `./install.sh --help` lists them all.

For a permanent address, in the Cloudflare dashboard (Zero Trust → Networks → Tunnels) create a tunnel,
copy its token, and add a public hostname whose service is **HTTP `localhost:41700`**.

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

The installer gives each Hermes profile its own scoped, revocable token (alerts, lists, briefings, artifacts
only) and Hermes talks to GACA over `127.0.0.1`, never the public address. Profiles you create later are wired
automatically. `uv run gaca-api hermes connect` re-runs it by hand; `gaca-api token list|revoke` manages the tokens.

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
