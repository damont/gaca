"""Encryption at rest for the cloud server token (and a link's device code).

The token lets this server act as the owner's cloud account, so it is never stored in the clear:
`cloud_link.token_enc` holds a Fernet token (AES-128-CBC + HMAC-SHA256, from `cryptography`).

Where the key comes from, first match wins:
  1. `GACA_CLOUD_KEY` (env / .env): one or more Fernet keys, comma-separated, NEWEST FIRST.
  2. A key file `cloud_link.key` in the data directory (the folder holding the database),
     created with mode 0600 on first use. One key per line, newest first.

Rotation (the old key keeps working until you drop it, so nothing is ever locked out):
  * key file:  `gaca-api cloud rotate-key` adds a new key at the top of the file and re-encrypts
    the stored token under it. Old keys stay in the file; delete them once you are sure no backup
    of the database from before the rotation is needed.
  * env:       generate a key (`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`),
    set GACA_CLOUD_KEY="<new>,<old>", restart, run `gaca-api cloud rotate-key`, then later
    set GACA_CLOUD_KEY="<new>".
Losing every key means the stored token is unreadable: unlink and link again (the cloud-side
token can be revoked from the website's "Linked servers"). Nothing else is lost.

A database backup alone does not contain the key when it comes from the env; with the key file
the file sits beside the database, so back it up separately from the database if you want the
link to survive a restore.
"""
from __future__ import annotations

import logging
import os
import secrets
import stat
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from ..config import settings

logger = logging.getLogger(__name__)

KEY_FILE_NAME = "cloud_link.key"


class CryptoError(Exception):
    """The stored secret cannot be read with the available key(s)."""


def key_file() -> Path:
    return Path(settings.db_path).resolve().parent / KEY_FILE_NAME


def _clean(keys: list[str]) -> list[str]:
    return [k.strip() for k in keys if k.strip() and not k.strip().startswith("#")]


def _read_keys() -> list[str]:
    if settings.cloud_key.strip():
        return _clean(settings.cloud_key.split(","))
    path = key_file()
    if not path.exists():
        _create_key_file(path)
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:  # somebody loosened it: tighten rather than carry on exposed
        os.chmod(path, 0o600)
        logger.warning("tightened permissions on the cloud key file to 0600")
    return _clean(path.read_text().splitlines())


def _create_key_file(path: Path) -> None:
    """Make the key file so that a reader can never see it half-written: write a temp file (0600
    from the first byte), fsync it, then hard-link it into place, which fails if somebody got there
    first (exactly one creator wins) and otherwise shows the complete file at once."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(Fernet.generate_key().decode() + "\n")
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(tmp, path)
        except FileExistsError:
            pass
    finally:
        os.unlink(tmp)


def _fernet() -> MultiFernet:
    keys = _read_keys()
    if not keys:
        raise CryptoError("no cloud encryption key is configured")
    try:
        return MultiFernet([Fernet(k.encode()) for k in keys])
    except (ValueError, TypeError):
        raise CryptoError("the cloud encryption key is not a valid Fernet key") from None


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        raise CryptoError("the stored cloud token cannot be decrypted with the current key(s)") from None


def new_primary_key() -> None:
    """Make a fresh key the primary one. File mode only: with an env key, put the new key first
    in GACA_CLOUD_KEY yourself (see the module docstring)."""
    if settings.cloud_key.strip():
        raise CryptoError("the key comes from GACA_CLOUD_KEY: put a new key first there, then re-encrypt")
    path = key_file()
    _read_keys()  # makes sure the file exists with the right mode
    old = path.read_text()
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(Fernet.generate_key().decode() + "\n" + old)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
