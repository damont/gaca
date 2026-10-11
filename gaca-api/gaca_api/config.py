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
        env_file=("../.env", ".env"), env_prefix="GACA_", extra="ignore"
    )

    # Requests over this many bytes are refused with 413 (the 2 MiB photo cap plus multipart
    # framing). Used to be nginx's client_max_body_size.
    max_body_bytes: int = 3 * 1024 * 1024

    # Attempts per minute, per client, on the endpoints a stranger can reach without
    # credentials: pairing a device with a code.
    auth_attempts_per_minute: int = 10

    # Browser origins allowed to call the API cross-origin, comma-separated. Empty (the default)
    # means none: the phone app is not a browser, and nothing here is meant to be called from a web page.
    cors_origins: str = ""

    # The address phones reach this server at (e.g. https://gaca.example.com). The server cannot
    # work it out for itself — a Cloudflare tunnel token does not say which hostname it serves — so
    # the installer asks once and saves it. `gaca-api pair` puts it in the QR code.
    public_url: str = ""

    # Interface this service binds to. Loopback by default (safe for local dev);
    # set GACA_API_HOST=0.0.0.0 on the Pi so other machines can reach it.
    api_host: str = "127.0.0.1"

    # Port this service binds to (intentionally uncommon, not 8080/8000).
    api_port: int = 41700

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
    # an inline setting, this IS a secret — a real credential the server signs
    # requests with — so it's a file path, not an inline value, matching how
    # the Cloudflare Tunnel token is handled: never committed, mounted in at
    # deploy time. Push is best-effort everywhere it's used: leaving this
    # unset disables sending (services/push.py no-ops) rather than failing
    # alert creation or delegation, which must never depend on it.
    fcm_credentials_path: str | None = None

    # iPhone push goes straight to Apple (APNs), not through Firebase: token-based
    # auth with a .p8 key from Apple Developer -> Certificates, Identifiers &
    # Profiles -> Keys (Apple Push Notifications service enabled). The key file is
    # a real secret — a path, mounted at deploy time, like the FCM one. iOS push is
    # disabled (services/apns.py no-ops) unless key path, key id and team id are all
    # set; Android/FCM is unaffected either way.
    apns_key_path: str | None = None
    apns_key_id: str | None = None
    apns_team_id: str | None = None
    # The iOS app's bundle id. APNs rejects a token sent under any other topic.
    apns_topic: str = "dev.monts.gaca"


settings = Settings()
