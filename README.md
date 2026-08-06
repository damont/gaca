# GACA

Local run & test instructions. Each numbered step is its own terminal.

## 1. Start Hermes (via Nix)

From the repo root:

```bash
nix develop -c hermes gateway run
```

## 2. Start the API

From `gaca-api/` (first time: `cp .env.example .env` and set `GACA_AGENT_API_KEY`
to Hermes' `API_SERVER_KEY` from `~/.hermes/.env`):

```bash
.venv/bin/uvicorn gaca_api.main:app --port 41700 --reload
```

## 3. Test with the dev scripts

In two more terminals, both from `gaca-api/`, load the helpers:

```bash
source scripts/dev.sh
```

Then:

- **Terminal A:** `gaca-new` — create a session
- **Terminal B:** `gaca-listen` — open the event stream (leave it running)
- **Terminal A:** `gaca-say Say hi in exactly three words` — watch events in Terminal B

Other commands: `gaca-sid`, `gaca-show`, `gaca-listen <seq>` (replay after a Last-Event-ID).
