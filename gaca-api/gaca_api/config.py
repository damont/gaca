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

    # --- Auth ---------------------------------------------------------
    # Signing key for every token we mint (browser sessions now, agent tokens
    # next). MUST be overridden in production: anyone holding this value can
    # forge a token for any user. Rotating it is also the only revocation we
    # have — it invalidates every outstanding token at once.
    # Keep any real value at 32+ bytes: PyJWT raises InsecureKeyLengthWarning
    # below that for HS256 (RFC 7518 §3.2). See .env.example for a generator.
    jwt_secret: str = "change-me-this-default-is-not-secret-and-too-short"
    jwt_algorithm: str = "HS256"

    # Browser session lifetime. Agent tokens pass their own, much longer expiry
    # per request rather than reading this.
    jwt_expire_minutes: int = 10080  # 7 days

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
