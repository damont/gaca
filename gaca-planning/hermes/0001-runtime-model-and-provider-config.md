# 0001 — Model & provider config is runtime, through the API

**Status:** Accepted · **Date:** 2026-07-04 · **Area:** gaca-api ↔ Hermes adapter

## Context

GACA is meant to be **distributable** — anyone can deploy it on their own box
and connect it to their Android app. That constraint changes what
"reproducible" means here:

- **Reproducible = the gaca-api and Hermes ship as matching, pinned versions**
  (one coherent artifact, via the Nix flake). It does **not** mean baking any
  particular operator's model choice or provider secrets into the flake.
- You cannot put someone else's Claude/OpenAI API key in a flake. Each deployer
  supplies their **own** provider credentials and picks their **own** model.

Therefore provider credentials and model selection are **per-deployment runtime
configuration**, entered by the operator **through the API** (ultimately the
Android app) — not declarative Nix/systemd config.

Hermes supports this cleanly: its config is plain files in a mutable state dir
(`~/.hermes/`, relocatable via `HERMES_HOME`) — secrets in `.env`, settings in
`config.yaml` — so it can be configured headlessly and programmatically, no
interactive wizard required.

## Decision

1. **Model selection, provider API keys, and model metadata are runtime config,
   set through gaca-api** — not through the deploy layer.

2. **The HermesAdapter owns applying that config to Hermes.** It writes provider
   keys and `API_SERVER_KEY` to `~/.hermes/.env`, and model/provider settings to
   `~/.hermes/config.yaml` (or the equivalent `hermes config set` path). This is
   the "first-time setup" the app performs itself.

3. **The AgentBackend port gains model-management capability.** The API/adapter
   must be able to:
   - **set/update the active model** (provider + model id + params),
   - **accept and store provider credentials** (API keys),
   - **know information about models** — list available models and expose their
     metadata/capabilities back to the app.

4. **Nix's role is narrowed** to pinning gaca-api + Hermes to the same version =
   the reproducible, distributable artifact. It does **not** provision provider
   secrets or model choice.

5. **Providers, phased:**
   - **Now:** Anthropic (Claude) and OpenAI — API-key providers, so first-run is
     fully headless and app-driven.
   - **Later:** local LLMs via **Ollama**, and **OpenRouter**.
   - **Out of scope for now:** interactive OAuth brains (e.g. Codex OAuth) —
     they can't be completed headlessly, which would break the app-driven setup.

## Consequences

- **Ports & adapters holds.** All Hermes-specific config writing (`.env`,
  `config.yaml`, key names, file paths) lives inside `agent/hermes/`. Routers and
  services speak a **neutral** model-config schema; only the adapter knows
  Hermes' shapes. `grep -ri hermes` outside the adapter still returns nothing.

- **New neutral schemas** (in `schemas/`), e.g.: `ModelConfig`
  (provider, model id, params), `ProviderCredential` (provider, key), `ModelInfo`
  (id, provider, context window, capabilities). The port grows methods along the
  lines of `set_model`, `set_provider_credential`, `list_models` /
  `get_model_info`.

- **First-run flow becomes app-driven:** deploy the pinned artifact → operator
  opens the app → enters a provider key + picks a model → adapter writes Hermes
  config → api_server is ready. No operator edits config files by hand.

- **Secrets path:** keys arrive over the (Cloudflare-protected) API edge and are
  written by the adapter to `~/.hermes/.env` with tight file permissions.
  Encryption-at-rest for stored keys is a later hardening step.

- **Reserve now, don't leak later:** put model-management + credential methods in
  the `AgentBackend` port from the start so adding Ollama/OpenRouter (or swapping
  the engine) later is not a contract change — same reasoning as the plan's
  `/capabilities` and approval-gate reservations.

## Verified against a live Hermes (2026-07-04)

Confirmed by standing up the Nix-built Hermes (`packages.x86_64-linux.default`)
and hitting the api_server by hand:

- **api_server is a gateway *platform*.** Enable it in `~/.hermes/.env`
  (`API_SERVER_ENABLED=true`, `API_SERVER_HOST=127.0.0.1`, `API_SERVER_PORT=8642`,
  `API_SERVER_KEY=…`) and run it with **`hermes gateway run`** (foreground). It is
  **not** `hermes serve` — that's a different server (desktop JSON-RPC/WebSocket,
  default `:9119`).
- **`API_SERVER_KEY` must be longer than 16 characters** — Hermes rejects a
  shorter key. The adapter must generate keys that satisfy this.
- On a **loopback** bind, `GET /health` and `GET /v1/models` are reachable
  **without** a bearer token; auth becomes mandatory only on a non-loopback bind.
  (We'll still send the bearer from the adapter regardless.)
- `/health` and `/v1/models` both return 200 with no provider configured yet;
  `/v1/runs` (the actual agent run) is the next thing to prove and needs a
  provider key + model set — i.e. it exercises this decision directly.

## Open questions (verify against a live Hermes)

- **Does the api_server pick up `.env` / `config.yaml` changes live, or does a
  model/key change require a Hermes restart or reload?** Determines whether the
  adapter needs to trigger a reload after writing config.
- **Where does model metadata come from** — Hermes' `/v1/models`, the provider's
  own API, or a curated static list maintained in the adapter?
- **Storage shape for multiple providers' keys** (one `.env` vs. a small managed
  store the adapter renders into `.env`).

## Related

- `../hermes-plan.html` — ports & adapters seam, locked decisions, leak-point
  checklist (this decision extends the "capabilities, not hardcoded features" and
  "neutral event schema" points to model/provider config).
- `0002-storage-boundary.md` — generalizes this ADR's storage split (runtime
  config through the API, never baked in) into the standing rule for every
  crew member fact: gaca-api owns identity/config, Hermes is always asked
  live for capability (skills now; models, once built, under this same ADR).
