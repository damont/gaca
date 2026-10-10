# 0004 — Local agent mode

**Status:** implemented (PR "Local agent mode"); production still on `ssh` until the host-process switch.

## Decision
GACA and Hermes live on one machine. `GACA_AGENT_ACCESS=local` replaces the per-user SSH login with
`provisioning/local.py`, which does exactly this and nothing more:

| Does | Limit |
|------|-------|
| Read `.env` (API_SERVER_* lines only), `SOUL.md`, `profile.yaml`, `config.yaml` (one flag), `default_soul.py` | fixed filenames, size-capped, regular files only, symlinks refused, profile names strictly patterned |
| Write one file | `profiles/<name>/.env` of a profile it just created; atomic, 0600 |
| Run commands | `hermes --version`, `hermes profile create <name> --no-alias`; allowlist checked before spawn, exec with argv (no shell), scrubbed env, killed on timeout |

The HTTP API is unchanged: discovery, creation and the purpose read are the same endpoints. Only
what sits behind them changed. In local mode an `access` block sent by a client is ignored and
never stored.

`default_soul.py` used to be *executed* (imported) over SSH. It is now parsed with a whitelist AST
evaluator; output is byte-identical on a live install, and a hostile file runs nothing.

## Why a host process, not a container
The API has to run the host's `hermes` CLI to create profiles. A container can't, so the options
were a host-side bridge service, a wide writable mount, or running the API on the host. Running on
the host is the simplest and the most testable. The cost is that a compromised API process runs as
the operator's user; the in-code limits above don't help against that, so `deploy/gaca-api.service`
adds an OS sandbox (`ProtectHome=tmpfs`, only the data dir and `profiles/` writable).

Be honest about the ceiling: gaca-api also holds each crew member's Hermes API key and can start
agent runs, and a Hermes agent can execute commands. The sandbox protects the rest of the machine
(SSH keys, other apps' files); it is not a guarantee against a compromised API steering Hermes.

## Needs a system unit
`systemctl --user` cannot apply the sandbox on Ubuntu 24.04+ (unprivileged user namespaces are
blocked by AppArmor; the options are silently ignored). Install it as a system unit with
`User=` set and run `deploy/check-sandbox.sh`, which fails if anything that should be blocked isn't.

## Not supported in local mode
Hermes older than 0.21 (one gateway per profile): creating a profile would need
`hermes gateway install`, which is outside the allowlist. Use `ssh` mode for those.
