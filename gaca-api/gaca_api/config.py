"""Single source of truth for gaca-api configuration.

Everything reads from `settings`. Values come from the environment (prefixed
GACA_) or a local .env file. This starts tiny on purpose — we add fields
(db_path, redis_url, model, ...) only as a slice actually needs them.
"""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="GACA_", extra="ignore"
    )

    # Single-user API key clients will send. Not enforced yet — wired in when we
    # add auth to the session endpoints.
    api_key: str = "dev-change-me"

    # Interface this service binds to. Loopback by default (safe for local dev);
    # set GACA_API_HOST=0.0.0.0 on the Pi so other machines can reach it.
    api_host: str = "127.0.0.1"

    # Port this service binds to (intentionally uncommon, not 8080/8000).
    api_port: int = 41700

    # Embedded SQLite is the system of record (sessions, messages; later audit,
    # events). One file, no server. Override per deployment with GACA_DB_PATH —
    # e.g. /var/lib/gaca/gaca.db on the Pi.
    db_path: str = "gaca.db"

    # The agent engine's HTTP gateway: where the adapter submits runs and reads
    # the run event stream, plus the bearer it authenticates with. Named
    # neutrally on purpose — the core never says *which* engine is behind it.
    agent_base_url: str = "http://127.0.0.1:8642"
    agent_api_key: str = ""

    # Which engine adapter to load (agent/<engine>/adapter.py). Set via
    # GACA_AGENT_ENGINE — kept out of code so the core names no specific engine.
    agent_engine: str = ""

    # Idle timeout (seconds) for the run event stream: if the engine sends no
    # event for this long, the run is treated as failed. Per-event, NOT total —
    # a long but active run won't trip it.
    agent_stream_idle_timeout: float = 30.0


settings = Settings()
