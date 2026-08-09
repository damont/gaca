# GACA

Local run & test instructions.

Every app route lives under **`/api`** — in production nginx proxies `location /api/`
to this service without stripping the prefix, so a route outside `/api` would be
unreachable through the front door.

## 1. Start the API

From `gaca-api/` (first time: `cp .env.example .env`):

```bash
uv sync
uv run uvicorn gaca_api.main:app --port 41700 --reload
```

No Nix needed. `flake.nix` still exists at the repo root — it's how Hermes was
installed — but it is not in the run loop for the API.

Check it's up:

```bash
curl localhost:41700/api/health
```

## 2. Test with the dev scripts

In another terminal, from `gaca-api/`:

```bash
source scripts/dev.sh
```

Then:

- `gaca-new` — create a session (prints and remembers the id)
- `gaca-show` — show the session and its stored messages
- `gaca-listen` — open the SSE event stream (leave it running; Ctrl-C to stop)

Other commands: `gaca-sid`, `gaca-listen <seq>` (replay after a Last-Event-ID).

Override the target with `GACA_BASE=http://host:port` before sourcing.

## 3. Chat — needs Hermes reachable

`gaca-say <text>` posts a message and starts an agent run, so it is the one
helper that needs the Hermes gateway.

**Hermes now runs on a separate box.** Until `GACA_AGENT_BASE_URL` points at that
box and the network path is open, `gaca-say` fails with a connection error. Note
that the user message is still persisted to SQLite first — `gaca-show` will show
an orphaned user turn with no assistant reply.

Everything else (sessions, transcripts, the event stream) is SQLite-only and
works with Hermes down.

To wire it up, set in `gaca-api/.env`:

```
GACA_AGENT_BASE_URL=http://<hermes-box>:8642
GACA_AGENT_API_KEY=<Hermes' API_SERVER_KEY from its ~/.hermes/.env>
```

The `hermes-run` / `hermes-health` helpers probe that gateway directly; point
them with `HERMES_BASE=http://<hermes-box>:8642`.
