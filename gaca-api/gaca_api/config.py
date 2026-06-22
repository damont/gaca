"""Single source of truth for gaca-api configuration.

Everything reads from `settings`. Values come from the environment (prefixed
GACA_) or a local .env file. This starts tiny on purpose — we add fields
(mongo_url, redis_url, model, ...) only as a slice actually needs them.
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

    # Port this service binds to (intentionally uncommon, not 8080/8000).
    api_port: int = 41700

    # MongoDB — the system of record (sessions, messages; later audit, runs).
    mongo_url: str = "mongodb://localhost:27017"
    mongo_db: str = "gaca"


settings = Settings()
