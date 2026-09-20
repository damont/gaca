"""SSH access to a user's Hermes box.

This is a materially bigger grant than a `CrewMember.api_key`: that key
reaches one scoped Hermes gateway over HTTP; an `SSHConnection` can run
ANY command on the box as `username`. Never used to serve a chat/delegate
call — only to provision or discover crew members. See
gaca-planning/hermes/0002-storage-boundary.md for where this sits relative
to everything else gaca-api stores.

Host key verification is deliberately off (`known_hosts=None`): there is no
trust-on-first-use store yet to pin a box's host key against on first
connect, and pinning nothing at all is simpler to reason about than pinning
it wrong. This is a known, accepted gap for a personal box on a private
network — revisit before this is ever used against something less trusted.
"""
from __future__ import annotations

import json
import shlex

import asyncssh

from ..models.user import SSHConnection


class SSHError(Exception):
    """Wraps any asyncssh failure (auth, connect, non-zero exit) into one
    exception type the caller doesn't need to know asyncssh's own hierarchy
    to catch."""


async def run(conn: SSHConnection, command: str, *, timeout: float = 20.0) -> str:
    """Run one command on the connected box and return its stdout.

    Raises SSHError on a connection/auth failure, a non-zero exit, or a
    timeout — the caller never has to distinguish those, since all three
    mean the same thing to a provisioning call: it didn't work.
    """
    try:
        async with asyncssh.connect(
            conn.host,
            port=conn.port,
            username=conn.username,
            client_keys=[asyncssh.import_private_key(conn.private_key)],
            known_hosts=None,
            connect_timeout=timeout,
        ) as ssh_conn:
            result = await ssh_conn.run(command, check=True, timeout=timeout)
            return result.stdout if isinstance(result.stdout, str) else ""
    except (OSError, asyncssh.Error) as exc:
        raise SSHError(f"{command!r} failed: {exc}") from exc


# Runs ON the box, over the connection above — reads (never writes) each
# profile's own .env for the four API_SERVER_* keys Hermes' gateway reads to
# serve the HTTP surface gaca-api talks to (confirmed live on david-claw,
# 2026-09-20: API_SERVER_ENABLED/HOST/PORT/KEY, one block per profile's own
# .env — the root ~/.hermes/.env for "default", ~/.hermes/profiles/<name>/.env
# for each named one). A profile with the API server off, or missing a port
# or key, is silently excluded — nothing for gaca-api to reach there yet.
_DISCOVER_SCRIPT = r"""
import json, pathlib, re

def read_api_server(env_path):
    if not env_path.is_file():
        return None
    values = {}
    for line in env_path.read_text(errors="replace").splitlines():
        m = re.match(r"^(API_SERVER_[A-Z_]+)=(.*)$", line)
        if m:
            values[m.group(1)] = m.group(2)
    if values.get("API_SERVER_ENABLED", "").strip().lower() != "true":
        return None
    port = values.get("API_SERVER_PORT", "").strip()
    key = values.get("API_SERVER_KEY", "").strip()
    if not port.isdigit() or not key:
        return None
    return {"port": int(port), "api_key": key}

home = pathlib.Path.home() / ".hermes"
found = {}
default = read_api_server(home / ".env")
if default:
    found["default"] = default
profiles_dir = home / "profiles"
if profiles_dir.is_dir():
    for entry in sorted(profiles_dir.iterdir()):
        if entry.is_dir():
            info = read_api_server(entry / ".env")
            if info:
                found[entry.name] = info
print(json.dumps(found))
"""


async def discover_profiles(conn: SSHConnection) -> dict[str, dict]:
    """Every profile on the box with its HTTP gateway enabled, by name —
    `{"toolshed": {"port": 8643, "api_key": "..."}, ...}`. Read-only: never
    creates, installs, or changes anything on the box.
    """
    stdout = await run(conn, f"python3 -c {shlex.quote(_DISCOVER_SCRIPT)}")
    return json.loads(stdout)


async def next_free_port(conn: SSHConnection, *, base: int = 8642) -> int:
    """One past the highest API_SERVER_PORT already in use across every
    profile on the box (default, toolshed, trading = 8642/8643/8644 on
    david-claw, 2026-09-20 — this generalizes that pattern), or `base` if
    none are configured yet. Reads the box's real state via
    `discover_profiles` rather than gaca-api's own database, which can be
    stale or simply not know about a profile someone set up by hand.
    """
    found = await discover_profiles(conn)
    ports = [info["port"] for info in found.values()]
    return max(ports, default=base - 1) + 1


# Runs ON the box, over the connection above — the confirmed-live command
# sequence (david-claw, 2026-09-20, `hermes --help`/`profile --help`/
# `gateway --help`/`config --help`, cross-checked against real behavior):
#   1. `hermes profile create <name> --no-alias` — no --port flag exists;
#      profile create never touches API_SERVER_*.
#   2. Upsert (never blind-append — a fresh profile's .env may already have
#      unrelated content) the four API_SERVER_* keys into that profile's own
#      .env (`hermes config set` only touches config.yaml-style keys, not
#      .env — confirmed via `hermes config --help`; env vars need a direct
#      file edit).
#   3. `hermes --profile <name> gateway install --start-now` — `--profile`
#      is a real global flag (confirmed: `hermes --profile toolshed config
#      env-path` correctly resolved that profile's own .env; also the
#      literal fix Hermes' own error log suggests: "hermes --profile default
#      gateway stop"). Installs the systemd/launchd service and starts it,
#      which then reads the .env we just wrote.
# Wrapped in `bash -lc` by create_profile below because `hermes` lives in
# ~/.local/bin, only on PATH in a login shell (confirmed: a plain non-login
# `ssh host hermes --version` fails with "not found"; `bash -lc` finds it).
_CREATE_PROFILE_SCRIPT = r"""
import json, pathlib, subprocess, sys

name, port, api_key = sys.argv[1], sys.argv[2], sys.argv[3]

created = subprocess.run(
    ["hermes", "profile", "create", name, "--no-alias"],
    capture_output=True, text=True,
)
if created.returncode != 0:
    print(json.dumps({"ok": False, "stage": "create", "error": created.stderr.strip()}))
    sys.exit(0)

env_path = pathlib.Path.home() / ".hermes" / "profiles" / name / ".env"
lines = env_path.read_text().splitlines() if env_path.is_file() else []
wanted = {
    "API_SERVER_ENABLED": "true",
    "API_SERVER_HOST": "0.0.0.0",
    "API_SERVER_PORT": port,
    "API_SERVER_KEY": api_key,
}
seen, out = set(), []
for line in lines:
    key = line.split("=", 1)[0] if "=" in line else None
    if key in wanted:
        out.append(f"{key}={wanted[key]}")
        seen.add(key)
    else:
        out.append(line)
for key, value in wanted.items():
    if key not in seen:
        out.append(f"{key}={value}")
env_path.parent.mkdir(parents=True, exist_ok=True)
env_path.write_text("\n".join(out) + "\n")

installed = subprocess.run(
    ["hermes", "--profile", name, "gateway", "install", "--start-now"],
    capture_output=True, text=True,
)
if installed.returncode != 0:
    print(json.dumps({"ok": False, "stage": "install", "error": installed.stderr.strip()}))
    sys.exit(0)

print(json.dumps({"ok": True}))
"""


async def create_profile(conn: SSHConnection, name: str, port: int, api_key: str) -> None:
    """Create a brand new Hermes profile, enable its HTTP gateway on `port`
    with `api_key`, and install+start its gateway service. Raises SSHError
    (with the real Hermes stderr) if any stage fails — most commonly the
    name already exists as a profile on the box.
    """
    inner = (
        f"python3 -c {shlex.quote(_CREATE_PROFILE_SCRIPT)} "
        f"{shlex.quote(name)} {port} {shlex.quote(api_key)}"
    )
    # 60s, not the default 20s — profile create bootstraps bundled skills,
    # which is slower than the one-liners every other command here runs.
    stdout = await run(conn, f"bash -lc {shlex.quote(inner)}", timeout=60.0)
    result = json.loads(stdout)
    if not result.get("ok"):
        raise SSHError(
            f"provisioning {name!r} failed at {result.get('stage')}: {result.get('error')}"
        )
