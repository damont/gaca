"""The server's .env: created once, never clobbered."""
from __future__ import annotations

import re
from pathlib import Path

from ..provisioning.envfile import read_text, upsert_env


def _read(path: Path) -> dict[str, str]:
    values = {}
    for line in read_text(path).splitlines():
        match = re.match(r"^([A-Z][A-Z0-9_]*)=(.*)$", line)
        if match:
            values[match.group(1)] = match.group(2)
    return values


def ensure_env(path: Path, *, data_dir: Path, port: int, hermes_home: Path, hermes_bin: str | None) -> list[str]:
    """Make sure `path` has everything the server needs, keeping what is already there.
    Returns human-readable notes about what changed. Nothing that is already set is overwritten."""
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
    if to_write:
        upsert_env(path, to_write)
        notes.append(f"wrote {len(to_write)} setting(s) to {path.name}: {', '.join(to_write)}")
    return notes or [f"{path.name} already complete"]
