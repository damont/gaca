"""Single source of truth for gaca-api configuration.

Everything reads from `settings`. Values come from the environment (prefixed
GACA_) or a local .env file. This starts tiny on purpose — we add fields
(db_path, redis_url, model, ...) only as a slice actually needs them.
"""
from __future__ import annotations

from typing import Literal

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

    # Google OAuth. One Client ID serves both sides: the API verifies the `aud`
    # claim on incoming ID tokens with it, and the frontend renders its button
    # with the same value (as VITE_GOOGLE_CLIENT_ID). Not a secret — it's public
    # by design — but leaving it unset disables Google sign-in entirely.
    google_client_id: str | None = None

    # Interface this service binds to. Loopback by default (safe for local dev);
    # set GACA_API_HOST=0.0.0.0 on the Pi so other machines can reach it.
    api_host: str = "127.0.0.1"

    # Port this service binds to (intentionally uncommon, not 8080/8000).
    api_port: int = 41700

    # Also listen on this Unix socket (in addition to api_host:api_port). Used when the API
    # runs as a host process and the frontend's nginx container proxies to it through a
    # mounted directory — see serve.py. Unset = TCP only.
    api_socket: str | None = None

    # Embedded SQLite is the system of record (sessions, messages; later audit,
    # events). One file, no server. Override per deployment with GACA_DB_PATH —
    # e.g. /var/lib/gaca/gaca.db on the Pi.
    db_path: str = "gaca.db"

    # Which engine adapter to load (agent/<engine>/adapter.py). Set via
    # GACA_AGENT_ENGINE — kept out of code so the core names no specific engine.
    # This is the only agent setting that's still app-wide: *which kind* of
    # engine gaca-api knows how to speak to. The host and per-member port/key
    # for a given engine are per-user (UserDoc.agent_host / UserDoc.crew,
    # managed via /api/crew) — every operator brings their own crew rather
    # than sharing one configured for the whole app.
    agent_engine: str = ""

    # Idle timeout (seconds) for the run event stream: if the engine sends no
    # event for this long, the run is treated as failed. Per-event, NOT total —
    # a long but active run won't trip it.
    agent_stream_idle_timeout: float = 180.0

    # How gaca-api reaches the machine Hermes runs on, for discovering and creating crew
    # members and reading what a crew member is for. "local": gaca-api runs on that same
    # machine (the supported setup) and reads/acts on HERMES_HOME directly through
    # provisioning/local.py, which can read a fixed set of Hermes files and run only
    # `hermes profile create` / `hermes --version`. "ssh": Hermes is on another machine and
    # each user supplies an SSH login (legacy; stores a private key per user).
    agent_access: Literal["ssh", "local"] = "ssh"
    hermes_home: str = "~/.hermes"
    # The hermes executable for local mode: a name looked up on PATH (and ~/.local/bin) or an
    # absolute path.
    hermes_bin: str = "hermes"

    # --- Push notifications --------------------------------------------
    # Path to a Firebase service-account JSON key (Firebase console → Project
    # settings → Service accounts → Generate new private key). Unlike
    # google_client_id, this IS a secret — a real credential the server signs
    # requests with — so it's a file path, not an inline value, matching how
    # the Cloudflare Tunnel token is handled: never committed, mounted in at
    # deploy time. Push is best-effort everywhere it's used: leaving this
    # unset disables sending (services/push.py no-ops) rather than failing
    # alert creation or delegation, which must never depend on it.
    fcm_credentials_path: str | None = None


settings = Settings()
