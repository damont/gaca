"""Wire Hermes to this GACA server: one scoped token per Hermes profile, written into that
profile's own .env, so no one ever copies a key by hand.

Run by the operator from a shell on the server (`gaca-api hermes connect`), not by the API
process: it writes into Hermes' files, which the sandboxed service deliberately cannot.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..config import settings
from ..models.api_token import ApiTokenDoc
from ..models.user import UserDoc
from ..services import tokens as token_service
from ..utils import tokens as token_utils
from .envfile import has_gaca_token, upsert_env
from .local import PROFILE_NAME


@dataclass
class Connected:
    profile: str
    env_path: Path
    token_name: str
    token_id: str | None  # None: already wired, left alone


def gaca_base_url() -> str:
    return f"http://127.0.0.1:{settings.api_port}"


async def mint_profile_env(owner: UserDoc, profile: str) -> tuple[ApiTokenDoc, dict[str, str]]:
    """A fresh scoped token for `profile` and the two env lines that hand it to that profile's skills."""
    doc, secret = await token_service.create(owner, f"hermes-{profile}", list(token_utils.SCOPES))
    return doc, {"GACA_BASE_URL": gaca_base_url(), "GACA_API_TOKEN": secret}


def profile_env_paths(home: Path) -> dict[str, Path]:
    """`{"default": ~/.hermes/.env, "toolshed": ~/.hermes/profiles/toolshed/.env, ...}` for the
    profiles that exist. Symlinked directories and oddly-named entries are skipped."""
    home = home.expanduser().resolve()
    found = {"default": home / ".env"} if home.is_dir() else {}
    profiles = home / "profiles"
    if profiles.is_dir() and not profiles.is_symlink():
        for entry in sorted(profiles.iterdir()):
            if entry.is_dir() and not entry.is_symlink() and PROFILE_NAME.fullmatch(entry.name):
                found[entry.name] = entry / ".env"
    return found


async def connect_profiles(
    home: Path,
    base_url: str,
    owner: UserDoc,
    scopes: list[str] | None = None,
    *,
    rotate: bool = False,
) -> list[Connected]:
    """Give each Hermes profile a scoped token (named `hermes-<profile>`) in its own .env.

    Safe to re-run: a profile that already holds a GACA API token is left alone unless `rotate`
    (old agent JWTs don't count, so they get replaced). With `rotate`, the profile's previous
    token is revoked after the new one is written."""
    scopes = scopes or list(token_utils.SCOPES)
    results = []
    for profile, env_path in profile_env_paths(home).items():
        name = f"hermes-{profile}"
        if has_gaca_token(env_path) and not rotate:
            results.append(Connected(profile, env_path, name, None))
            continue
        # Write the new token before revoking the old one: if the write fails, the old one still works.
        doc, secret = await token_service.create(owner, name, scopes)
        try:
            upsert_env(env_path, {"GACA_BASE_URL": base_url, "GACA_API_TOKEN": secret})
        except BaseException:
            # The secret never reached a file, so nobody holds it: don't leave it valid.
            await token_service.revoke(owner, doc.id)
            raise
        await token_service.revoke_older_with_name(owner, name, keep_id=doc.id)
        results.append(Connected(profile, env_path, name, doc.id))
    return results
