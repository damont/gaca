"""Wire Hermes to this GACA server: one scoped token per Hermes profile, written into that
profile's own .env, so no one ever copies a key by hand.

Run by the operator from a shell on the server (`gaca-api hermes connect`), not by the API
process: it writes into Hermes' files, which the sandboxed service deliberately cannot.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..models.user import UserDoc
from ..services import tokens as token_service
from ..utils import tokens as token_utils
from .envfile import upsert_env
from .local import PROFILE_NAME


@dataclass
class Connected:
    profile: str
    env_path: Path
    token_name: str
    token_id: str


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
    home: Path, base_url: str, owner: UserDoc, scopes: list[str] | None = None
) -> list[Connected]:
    """Mint a token for each profile (named `hermes-<profile>`, revoking its predecessor) and
    write GACA_BASE_URL + GACA_API_TOKEN into the profile's .env. Re-running rotates the tokens."""
    scopes = scopes or list(token_utils.SCOPES)
    results = []
    for profile, env_path in profile_env_paths(home).items():
        name = f"hermes-{profile}"
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
