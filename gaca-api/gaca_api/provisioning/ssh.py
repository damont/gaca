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
