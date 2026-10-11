"""Is Hermes on this machine, and set up the way GACA needs?

GACA is the phone app for a Hermes agent, so Hermes is a requirement, not an option. GACA does not
install or configure Hermes (it has its own installer, its own upgrades and your own model
credentials). This module only checks what GACA depends on and says, concretely, what is missing:

  * the `hermes` command, version 0.21 or newer (one gateway serves every profile);
  * a Hermes home (`hermes setup` creates it, and is where you choose a model provider);
  * the gateway's API server switched on: API_SERVER_ENABLED=true and an API_SERVER_KEY in
    ~/.hermes/.env (GACA chats through it);
  * the gateway actually running and answering on its port (8642 unless API_SERVER_PORT says otherwise).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ..provisioning.envfile import read_text
from .shell import Shell

MIN_VERSION = (0, 21)
OFFICIAL_INSTALL = "curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash"
DOCS_API_SERVER = "https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server"
DEFAULT_GATEWAY_PORT = 8642

_VERSION = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")
_TRUE = {"true", "1", "yes", "on"}


@dataclass
class Status:
    binary: str | None = None
    version: tuple[int, int, int] | None = None
    home_exists: bool = False
    api_enabled: bool = False
    api_key_set: bool = False
    port: int = DEFAULT_GATEWAY_PORT
    gateway_up: bool = False


@dataclass
class Problem:
    what: str
    fix: list[str] = field(default_factory=list)


def _env_values(home: Path) -> dict[str, str]:
    values = {}
    for line in read_text(home / ".env").splitlines():
        match = re.match(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=\s*(.*?)\s*$", line)
        if match:
            values[match.group(1)] = match.group(2).strip("'\"")
    return values


def inspect(shell: Shell, home: Path, binary: str | None) -> Status:
    status = Status(binary=binary, home_exists=home.is_dir())
    if binary:
        out = shell.run([binary, "--version"], timeout=30)
        found = _VERSION.search((out.stdout or "") + (out.stderr or "")) if out.returncode == 0 else None
        if found:
            status.version = tuple(int(part) for part in found.groups())  # type: ignore[assignment]
    env = _env_values(home)
    status.api_enabled = env.get("API_SERVER_ENABLED", "").lower() in _TRUE
    status.api_key_set = bool(env.get("API_SERVER_KEY"))
    try:
        status.port = int(env.get("API_SERVER_PORT") or DEFAULT_GATEWAY_PORT)
    except ValueError:
        status.port = DEFAULT_GATEWAY_PORT
    code, _ = shell.http_get(f"http://127.0.0.1:{status.port}/health")
    status.gateway_up = code == 200
    return status


def problems(status: Status, home: Path) -> list[Problem]:
    """What stands between this machine and a working GACA, most basic first. Empty = ready."""
    if not status.binary:
        return [Problem(
            "Hermes is not installed on this machine. GACA is the phone app for a Hermes agent, so it needs one here.",
            [
                "Install Hermes with its own installer:",
                f"    {OFFICIAL_INSTALL}",
                "then choose your model provider:",
                "    hermes setup",
                "and run ./install.sh again (it picks up where it left off).",
            ],
        )]
    found: list[Problem] = []
    if status.version is None or status.version[:2] < MIN_VERSION:
        shown = ".".join(map(str, status.version)) if status.version else "an unknown version"
        found.append(Problem(
            f"GACA needs Hermes {MIN_VERSION[0]}.{MIN_VERSION[1]} or newer (found {shown}). Older versions run one gateway per "
            "profile, which GACA does not support.",
            ["Update Hermes (re-running its installer updates it), then run ./install.sh again."],
        ))
    if not status.home_exists:
        found.append(Problem(
            f"Hermes has not been set up yet ({home} does not exist).",
            ["Run:", "    hermes setup", "to choose your model provider, then run ./install.sh again."],
        ))
        return found
    if not (status.api_enabled and status.api_key_set):
        found.append(Problem(
            "Hermes's API server is not switched on, which is how GACA talks to your agent.",
            [
                f"Add these two lines to {home}/.env (use any long random string for the key, e.g. from",
                "    python3 -c \"import secrets; print(secrets.token_urlsafe(32))\"):",
                "    API_SERVER_ENABLED=true",
                "    API_SERVER_KEY=<your long random string>",
                "Then start or restart Hermes's gateway, usually:",
                "    hermes gateway install        # once, to run it as a background service",
                "    systemctl --user restart hermes-gateway",
                f"More: {DOCS_API_SERVER}",
            ],
        ))
    elif not status.gateway_up:
        found.append(Problem(
            f"Hermes's gateway is not answering on 127.0.0.1:{status.port}.",
            [
                "Start it, usually:",
                "    systemctl --user start hermes-gateway",
                "(or run `hermes gateway` in a terminal to see why it will not start), then run ./install.sh again.",
            ],
        ))
    return found
