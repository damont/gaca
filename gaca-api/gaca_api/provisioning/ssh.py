"""SSH access to a user's Hermes box — only used when GACA_AGENT_ACCESS=ssh (Hermes on a
different machine from gaca-api). On the same machine, provisioning/local.py is used
instead and none of this runs.

This is a materially bigger grant than a `CrewMember.api_key`: that key
reaches one scoped Hermes gateway over HTTP; an `SSHConnection` can run
ANY command on the box as `username`. Never used to serve a chat/delegate
call — only to provision or discover crew members, plus ONE read-only,
fixed-command exception: reading a profile's SOUL.md and profile.yaml so the
app can show what a crew member is for (read_profile_files; Hermes serves
that only from its loopback-only, authenticated dashboard, which gaca-api can't
and shouldn't reach). See gaca-planning/hermes/0002-storage-boundary.md for
where this sits relative to everything else gaca-api stores, and for that
exception.

Host key verification is deliberately off (`known_hosts=None`): there is no
trust-on-first-use store yet to pin a box's host key against on first
connect, and pinning nothing at all is simpler to reason about than pinning
it wrong. This is a known, accepted gap for a personal box on a private
network — revisit before this is ever used against something less trusted.
"""
from __future__ import annotations

import json
import re
import shlex

import asyncssh

from ..models.user import SSHConnection
from .base import ProvisioningError


class SSHError(ProvisioningError):
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
#
# WHERE a profile is reached depends on how Hermes is run, and that changed
# (v0.21, 2026-10-03: "one gateway per host serves every profile"). Older Hermes
# ran a gateway per profile on the port in its own .env. Current Hermes serves the
# others from the DEFAULT gateway's port under /p/<name>, and a profile's own
# .env port is then stale. Hermes doesn't advertise which, so ask it: if the
# default gateway answers /p/<name>/health, that profile is served there.
_DISCOVER_SCRIPT = r"""
import json, pathlib, re, urllib.request

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

def served_by_default(default_port, name):
    try:
        url = "http://127.0.0.1:%d/p/%s/health" % (default_port, name)
        with urllib.request.urlopen(url, timeout=3) as response:
            return response.status == 200
    except Exception:
        return False

home = pathlib.Path.home() / ".hermes"
found = {}
default = read_api_server(home / ".env")
if default:
    found["default"] = dict(default, path="")
profiles_dir = home / "profiles"
if profiles_dir.is_dir():
    for entry in sorted(profiles_dir.iterdir()):
        if entry.is_dir():
            info = read_api_server(entry / ".env")
            if not info:
                continue
            if default and served_by_default(default["port"], entry.name):
                info = {"port": default["port"], "api_key": info["api_key"], "path": "/p/" + entry.name}
            else:
                info["path"] = ""
            found[entry.name] = info
print(json.dumps(found))
"""


async def discover_profiles(conn: SSHConnection) -> dict[str, dict]:
    """Every profile on the box with its HTTP gateway enabled, by name, with
    where it is actually reached — `{"toolshed": {"port": 8642, "path": "/p/toolshed",
    "api_key": "..."}, ...}` (`path` is "" for a profile with a gateway of its own).
    Read-only: never creates, installs, or changes anything on the box.
    """
    stdout = await run(conn, f"python3 -c {shlex.quote(_DISCOVER_SCRIPT)}")
    return json.loads(stdout)


# Runs ON the box: reads (never writes) the two small files that say what a profile is for —
# SOUL.md (its standing instructions) and profile.yaml (an optional one-line description).
# `default` lives in ~/.hermes itself; every other profile in ~/.hermes/profiles/<name>.
# The name is checked against a strict pattern here AND by the caller, so it can only ever
# select a directory directly under profiles/, never a path.
_READ_PROFILE_SCRIPT = r"""
import json, pathlib, re, sys

name = sys.argv[1]
home = pathlib.Path.home() / ".hermes"
if name == "default":
    d = home
elif re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name):
    d = home / "profiles" / name
else:
    d = None

def read(path, limit):
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit] if path.is_file() else None
    except OSError:
        return None

def stock_texts():
    # The installed Hermes' own list of "this persona carries no user intent" texts, so we
    # recognise them whatever version is installed (they changed between 0.20 and 0.21).
    # Loads just that one dependency-free file by path; never imports the hermes_cli package.
    try:
        import importlib.util
        path = home / "hermes-agent" / "hermes_cli" / "default_soul.py"
        spec = importlib.util.spec_from_file_location("hermes_default_soul", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        texts = [module.DEFAULT_SOUL_MD, *getattr(module, "_LEGACY_TEMPLATE_SOULS", ())]
        return [t for t in texts if isinstance(t, str)]
    except Exception:
        return []

if d is None or not d.is_dir():
    print(json.dumps({"found": False}))
else:
    print(json.dumps({"found": True, "soul": read(d / "SOUL.md", 40000),
                      "profile_yaml": read(d / "profile.yaml", 4000), "stock": stock_texts()}))
"""

_PROFILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


async def read_profile_files(conn: SSHConnection, name: str) -> dict | None:
    """`{"soul", "profile_yaml", "stock"}` for one profile (`stock`: the installed Hermes' own
    untouched-default persona texts, possibly empty), or None if no such
    profile exists on the box. Read-only; the command is fixed and `name` is validated."""
    if name != "default" and not _PROFILE_NAME.fullmatch(name):
        raise SSHError(f"not a valid profile name: {name!r}")
    stdout = await run(
        conn, f"python3 -c {shlex.quote(_READ_PROFILE_SCRIPT)} {shlex.quote(name)}", timeout=10.0
    )
    data = json.loads(stdout)
    return data if data.get("found") else None


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


# Runs ON the box, over the connection above. The confirmed-live command
# sequence (david-claw, 2026-09-20, `hermes --help`/`profile --help`/
# `gateway --help`/`config --help`, cross-checked against real behavior):
#   1. `hermes profile create <name> --no-alias` — no --port flag exists;
#      profile create never touches API_SERVER_*.
#   2. Upsert (never blind-append — a fresh profile's .env may already have
#      unrelated content) the four API_SERVER_* keys into that profile's own
#      .env (`hermes config set` only touches config.yaml-style keys, not
#      .env — confirmed via `hermes config --help`; env vars need a direct
#      file edit).
#   3. Make it reachable — which way depends on the Hermes (see _DISCOVER_SCRIPT):
#      * current Hermes (multiplexed): nothing to install. The default gateway
#        rescans profiles/ and serves the new one at /p/<name> within ~30s (2026-10-03,
#        v0.21.5, confirmed live). We wait for /p/<name>/health, and that is its address.
#      * older Hermes: `hermes --profile <name> gateway install --start-now`
#        installs and starts a gateway of its own on the port we wrote.
#      Which one it is gets decided by asking the box, not by version number: if the
#      default gateway already serves another named profile at /p/<it>, or the
#      new profile appears there within the wait, it's multiplexed.
# Wrapped in `bash -lc` by create_profile below because `hermes` lives in
# ~/.local/bin, only on PATH in a login shell (confirmed: a plain non-login
# `ssh host hermes --version` fails with "not found"; `bash -lc` finds it).
_CREATE_PROFILE_SCRIPT = r"""
import json, pathlib, re, subprocess, sys, time, urllib.request

name, port, api_key = sys.argv[1], sys.argv[2], sys.argv[3]
home = pathlib.Path.home() / ".hermes"

def fail(stage, error):
    print(json.dumps({"ok": False, "stage": stage, "error": error}))
    sys.exit(0)

def env_value(path, key):
    try:
        for line in path.read_text(errors="replace").splitlines():
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""

def health_ok(default_port, profile):
    try:
        url = "http://127.0.0.1:%d/p/%s/health" % (default_port, profile)
        with urllib.request.urlopen(url, timeout=3) as response:
            return response.status == 200
    except Exception:
        return False

created = subprocess.run(
    ["hermes", "profile", "create", name, "--no-alias"],
    capture_output=True, text=True,
)
if created.returncode != 0:
    fail("create", created.stderr.strip())

# Is this host multiplexed? Ask the default gateway whether it already serves another profile.
default_port_text = env_value(home / ".env", "API_SERVER_PORT")
default_port = int(default_port_text) if default_port_text.isdigit() else 8642
others = [p.name for p in sorted((home / "profiles").iterdir()) if p.is_dir() and p.name != name]
multiplexed = any(health_ok(default_port, other) for other in others)

env_path = home / "profiles" / name / ".env"
lines = env_path.read_text().splitlines() if env_path.is_file() else []
wanted = {
    "API_SERVER_ENABLED": "true",
    "API_SERVER_HOST": "0.0.0.0",
    "API_SERVER_PORT": str(default_port) if multiplexed else port,
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
env_path.chmod(0o600)

def expects_multiplex():
    # Only a tie-breaker for the FIRST named profile on a host (nothing to compare against): an
    # explicit flag in the default config, or a Hermes new enough (>= 0.21) to do it by default.
    try:
        text = (home / "config.yaml").read_text(errors="replace")
        if re.search(r"^\s*multiplex_profiles:\s*true\b", text, re.M):
            return True
    except OSError:
        pass
    try:
        out = subprocess.run(["hermes", "--version"], capture_output=True, text=True, timeout=20).stdout
        match = re.search(r"v?0\.(\d+)\.", out)
        return bool(match and int(match.group(1)) >= 21)
    except Exception:
        return False

if not multiplexed:
    # Give the default gateway a moment to notice the new profile before concluding it needs a
    # gateway of its own — but only on a host that should be multiplexed, so an older Hermes
    # isn't made to wait for nothing.
    deadline = time.time() + (40 if (not others and expects_multiplex()) else 0)
    while time.time() < deadline and not multiplexed:
        multiplexed = health_ok(default_port, name)
        if not multiplexed:
            time.sleep(2)

if multiplexed:
    deadline = time.time() + 60  # the default gateway rescans profiles/ about every 30s
    while time.time() < deadline and not health_ok(default_port, name):
        time.sleep(2)
    if not health_ok(default_port, name):
        fail("serve", "the default gateway did not start serving the new profile within 60s")
    print(json.dumps({"ok": True, "port": default_port, "path": "/p/" + name}))
else:
    installed = subprocess.run(
        ["hermes", "--profile", name, "gateway", "install", "--start-now"],
        capture_output=True, text=True,
    )
    if installed.returncode != 0:
        fail("install", installed.stderr.strip())
    print(json.dumps({"ok": True, "port": int(port), "path": ""}))
"""


async def create_profile(
    conn: SSHConnection, name: str, port: int, api_key: str
) -> tuple[int, str]:
    """Create a brand new Hermes profile with `api_key` and make it reachable, returning
    the `(port, path)` it can be reached at.

    On a Hermes that serves every profile from its default gateway that is the default's
    port and `/p/<name>` (nothing to install — `port` here is ignored); on an older one
    that runs a gateway per profile it is `port` and "" after installing and starting that
    gateway. Raises SSHError (with the real Hermes stderr) if any stage fails — most
    commonly the name already exists as a profile on the box.
    """
    if not _PROFILE_NAME.fullmatch(name) or name == "default":
        raise SSHError(f"not a valid new profile name: {name!r}")
    inner = (
        f"python3 -c {shlex.quote(_CREATE_PROFILE_SCRIPT)} "
        f"{shlex.quote(name)} {port} {shlex.quote(api_key)}"
    )
    # Generous: profile create bootstraps bundled skills, and the default gateway can take
    # ~30s to notice a new profile — far longer than the one-liners every other command runs.
    stdout = await run(conn, f"bash -lc {shlex.quote(inner)}", timeout=150.0)
    result = json.loads(stdout)
    if not result.get("ok"):
        raise SSHError(
            f"provisioning {name!r} failed at {result.get('stage')}: {result.get('error')}"
        )
    return int(result["port"]), result.get("path", "")


class SshProvisioner:
    """The module-level functions above, bound to one stored connection, so callers can
    treat SSH and local access the same way (see base.Provisioner)."""

    def __init__(self, conn: SSHConnection):
        self.conn = conn
        self.default_agent_host = f"http://{conn.host}"

    async def discover_profiles(self) -> dict[str, dict]:
        return await discover_profiles(self.conn)

    async def read_profile_files(self, name: str) -> dict | None:
        return await read_profile_files(self.conn, name)

    async def next_free_port(self) -> int:
        return await next_free_port(self.conn)

    async def create_profile(self, name: str, port: int, api_key: str) -> tuple[int, str]:
        return await create_profile(self.conn, name, port, api_key)
