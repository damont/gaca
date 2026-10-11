"""The server's .env: created once, never clobbered."""
from __future__ import annotations

import re
import secrets
from pathlib import Path

from ..config import DEFAULT_JWT_SECRET
from ..provisioning.envfile import read_text, upsert_env
from ..security import MIN_SECRET_LENGTH


def _read(path: Path) -> dict[str, str]:
    values = {}
    for line in read_text(path).splitlines():
        match = re.match(r"^([A-Z][A-Z0-9_]*)=(.*)$", line)
        if match:
            values[match.group(1)] = match.group(2)
    return values


def ensure_env(path: Path, *, data_dir: Path, port: int, hermes_home: Path, hermes_bin: str | None) -> list[str]:
    """Make sure `path` has everything the server needs, keeping what is already there.
    Returns human-readable notes about what changed. An existing strong JWT secret is NEVER
    replaced (that would sign the owner out); a missing or placeholder one is."""
    existing = _read(path)
    notes = []
    wanted = {
        "GACA_DB_PATH": str(data_dir / "gaca.db"),
        "GACA_AGENT_ENGINE": "hermes",
        "GACA_AGENT_ACCESS": "local",
        "GACA_HERMES_HOME": str(hermes_home),
        "GACA_API_HOST": "127.0.0.1",
        "GACA_API_PORT": str(port),
    }
    if hermes_bin:
        wanted["GACA_HERMES_BIN"] = hermes_bin
    to_write = {k: v for k, v in wanted.items() if k not in existing}
    secret = existing.get("GACA_JWT_SECRET", "")
    if not secret or secret == DEFAULT_JWT_SECRET or len(secret) < MIN_SECRET_LENGTH:
        to_write["GACA_JWT_SECRET"] = secrets.token_urlsafe(48)
        notes.append("generated a new JWT secret" if not secret else "replaced a weak JWT secret (existing sign-ins end)")
    if to_write:
        upsert_env(path, to_write)
        notes.append(f"wrote {len(to_write)} setting(s) to {path.name}: {', '.join(k for k in to_write if k != 'GACA_JWT_SECRET') or 'secret only'}")
    return notes or [f"{path.name} already complete"]
