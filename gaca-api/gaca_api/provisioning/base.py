"""What the rest of gaca-api may ask of "the machine Hermes runs on".

Two implementations: `local.LocalHermes` (gaca-api runs on the same machine as
Hermes — the default direction) and `ssh.SshProvisioner` (Hermes on another
box, kept behind GACA_AGENT_ACCESS=ssh). Callers get one through
`select.provisioner_for` and never care which.
"""
from __future__ import annotations

from typing import Protocol


class ProvisioningError(Exception):
    """Any failure reaching or acting on the Hermes machine. Mapped to HTTP 502
    in main.py: gaca-api acted on the operator's own box and that failed — not a
    client error, and not gaca-api's own fault either."""


class Provisioner(Protocol):
    # Where the Hermes HTTP gateway is reached from gaca-api when nothing is
    # connected yet ("http://127.0.0.1" locally, the SSH host otherwise).
    default_agent_host: str
    # True if this provisioner can put a GACA token into a profile's .env (local mode only; over
    # SSH the operator wires Hermes themselves).
    can_wire_gaca: bool

    async def discover_profiles(self) -> dict[str, dict]:
        """`{name: {"port", "path", "api_key"}}` for every profile with its HTTP gateway on."""
        ...

    async def read_profile_files(self, name: str) -> dict | None:
        """`{"found", "soul", "profile_yaml", "stock"}` for one profile, or None if absent."""
        ...

    async def read_skill_files(self, name: str) -> list[dict] | None:
        """Every `SKILL.md` under one profile's skills/ folder, as `{"path": [folders], "text"}`
        (path relative to skills/, text size-capped), or None if no such profile. Read-only."""
        ...

    async def read_skill_usage(self, name: str) -> str | None:
        """The text of one profile's skill-usage counter file (size-capped), or None when the
        profile or the file doesn't exist. Read-only; the engine's adapter parses it."""
        ...

    async def next_free_port(self) -> int: ...

    async def create_profile(
        self, name: str, port: int, api_key: str, extra_env: dict[str, str] | None = None
    ) -> tuple[int, str]:
        """Create a new profile and make it reachable; returns `(port, path)`."""
        ...

    async def profiles_missing_gaca_token(self) -> list[str]: ...

    async def write_gaca_env(self, name: str, values: dict[str, str]) -> None: ...

    async def named_profiles(self) -> list[str]:
        """Every named (non-default) profile that exists on the box, gateway or not."""
        ...

    async def install_skills(self, name: str) -> None:
        """Put GACA's Hermes skills into a named profile (local mode only)."""
        ...
