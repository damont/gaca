"""Push notifications: optional, per platform, and asked about during setup.

Without push, alerts still show up in the app whenever it is open; push is what buzzes the phone.
Android pushes go through Firebase (a service-account key file); iPhone pushes go straight to Apple
(an APNs key file plus its key id, your team id and the app's bundle id). A person can set up either,
both, or neither, and can add one later by running ./install.sh again.

Important and said plainly in the prompts: the credentials must belong to the same developer account
the app on the phone was built with (a push key can only message the app it was made for), so this is
for people who build the app themselves. Anyone using someone else's build gets alerts in the app but
cannot push to it.

Secret files are copied into `<config dir>/secrets/` (0700, files 0600), the folder the service's
sandbox is allowed to read, and only their paths go in `.env`. Nothing secret is ever printed.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path

from ..provisioning.envfile import read_text, remove_env, upsert_env

FCM_FILE = "fcm-service-account.json"
APNS_FILE = "apns-key.p8"
_MAX_FILE = 16_000
_APPLE_ID = re.compile(r"[A-Z0-9]{10}")
_BUNDLE_ID = re.compile(r"[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+")
_PEM = re.compile(r"-----BEGIN PRIVATE KEY-----[\sA-Za-z0-9+/=]+-----END PRIVATE KEY-----")


class PushSetupError(ValueError):
    """What was given is not usable; the message says what is wrong (never echoes a secret)."""


def secrets_dir(config_dir: Path) -> Path:
    return config_dir / "secrets"


def _read_small(path: Path) -> str:
    path = Path(path).expanduser()
    try:
        info = path.stat()
    except OSError as error:
        raise PushSetupError(f"cannot read {path}: {error.strerror or error}") from None
    if not stat.S_ISREG(info.st_mode):
        raise PushSetupError(f"{path} is not a file")
    if info.st_size > _MAX_FILE:
        raise PushSetupError(f"{path} is too large to be a key file")
    return path.read_text(errors="replace")


def check_fcm(path: Path) -> str:
    """The Firebase project id if `path` is a service-account key, else PushSetupError."""
    try:
        data = json.loads(_read_small(path))
    except json.JSONDecodeError:
        raise PushSetupError(f"{path} is not valid JSON; it should be the key file downloaded from Firebase "
                             "(Project settings > Service accounts > Generate new private key)") from None
    if not isinstance(data, dict) or data.get("type") != "service_account":
        raise PushSetupError(f"{path} is not a Firebase service-account key (its \"type\" should be \"service_account\")")
    for field in ("project_id", "client_email", "private_key"):
        if not isinstance(data.get(field), str) or not data[field]:
            raise PushSetupError(f"{path} is missing \"{field}\"; download a fresh service-account key from Firebase")
    if not data["private_key"].lstrip().startswith("-----BEGIN PRIVATE KEY-----"):
        raise PushSetupError(f"{path} does not contain a private key")
    return data["project_id"]


def check_apns_key(path: Path) -> None:
    if not _PEM.search(_read_small(path)):
        raise PushSetupError(f"{path} is not an Apple APNs key; it should be the .p8 file from the Apple Developer "
                             "portal (Certificates, Identifiers & Profiles > Keys)")


def check_apple_id(value: str, what: str) -> str:
    value = (value or "").strip().upper()
    if not _APPLE_ID.fullmatch(value):
        raise PushSetupError(f"the {what} is 10 letters and digits (for example ABC123DEFG)")
    return value


def check_bundle_id(value: str) -> str:
    value = (value or "").strip()
    if not _BUNDLE_ID.fullmatch(value):
        raise PushSetupError("the app's bundle id looks like com.example.gaca")
    return value


def install_secret(source: Path, directory: Path, name: str) -> Path:
    """Copy `source` to `<directory>/<name>` (0600, atomically), in a 0700 directory."""
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".push.")
    try:
        with os.fdopen(fd, "wb") as out, open(Path(source).expanduser(), "rb") as src:
            shutil.copyfileobj(src, out)
        os.chmod(tmp, 0o600)
        target = directory / name
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return target


def read_env(path: Path) -> dict[str, str]:
    values = {}
    for line in read_text(path).splitlines():
        match = re.match(r"^([A-Z][A-Z0-9_]*)=(.*)$", line)
        if match:
            values[match.group(1)] = match.group(2)
    return values


def android_ready(env: dict[str, str]) -> bool:
    path = env.get("GACA_FCM_CREDENTIALS_PATH", "")
    return bool(path) and Path(path).is_file()


def ios_ready(env: dict[str, str]) -> bool:
    path = env.get("GACA_APNS_KEY_PATH", "")
    return bool(path) and Path(path).is_file() and bool(env.get("GACA_APNS_KEY_ID") and env.get("GACA_APNS_TEAM_ID"))


EXPLAIN = (
    "Push notifications are optional. Without them, alerts appear in the app whenever you open it; push is what buzzes your phone.",
    "The credentials must come from the SAME developer account your app was built with (a push key can only message the app it was",
    "made for), so this is for people who build the app themselves. You can set up Android, iPhone, or both, and add one later by",
    "running ./install.sh again.",
)


def setup_android(env_file: Path, directory: Path, source: Path) -> str:
    """Validate and install the Firebase key, and point `.env` at it. Returns the Firebase project id."""
    project = check_fcm(source)
    target = install_secret(source, directory, FCM_FILE)
    upsert_env(env_file, {"GACA_FCM_CREDENTIALS_PATH": str(target)})
    return project


def setup_ios(env_file: Path, directory: Path, source: Path, key_id: str, team_id: str, bundle_id: str) -> None:
    check_apns_key(source)
    key_id, team_id, bundle_id = check_apple_id(key_id, "key id"), check_apple_id(team_id, "team id"), check_bundle_id(bundle_id)
    target = install_secret(source, directory, APNS_FILE)
    upsert_env(env_file, {
        "GACA_APNS_KEY_PATH": str(target), "GACA_APNS_KEY_ID": key_id,
        "GACA_APNS_TEAM_ID": team_id, "GACA_APNS_TOPIC": bundle_id,
    })


def remove_android(env_file: Path, directory: Path) -> bool:
    """Turn Android push off: drop the setting and delete our copy of the key. True if it was on."""
    removed = remove_env(env_file, ["GACA_FCM_CREDENTIALS_PATH"])
    (directory / FCM_FILE).unlink(missing_ok=True)
    return bool(removed)


def remove_ios(env_file: Path, directory: Path) -> bool:
    removed = remove_env(env_file, ["GACA_APNS_KEY_PATH", "GACA_APNS_KEY_ID", "GACA_APNS_TEAM_ID", "GACA_APNS_TOPIC"])
    (directory / APNS_FILE).unlink(missing_ok=True)
    return bool(removed)


def describe_android(env: dict[str, str]) -> str:
    """One line for `push status`: on (with the Firebase project), off, or broken."""
    path = env.get("GACA_FCM_CREDENTIALS_PATH", "")
    if not path:
        return "off"
    try:
        return f"on (Firebase project {check_fcm(Path(path))})"
    except PushSetupError as error:
        return f"BROKEN: {error}"


def describe_ios(env: dict[str, str]) -> str:
    path = env.get("GACA_APNS_KEY_PATH", "")
    if not (path or env.get("GACA_APNS_KEY_ID") or env.get("GACA_APNS_TEAM_ID")):
        return "off"
    try:
        check_apns_key(Path(path))
        if not (env.get("GACA_APNS_KEY_ID") and env.get("GACA_APNS_TEAM_ID")):
            raise PushSetupError("the Key ID or Team ID is missing")
    except PushSetupError as error:
        return f"BROKEN: {error}"
    return f"on (key {env['GACA_APNS_KEY_ID']}, team {env['GACA_APNS_TEAM_ID']}, app {env.get('GACA_APNS_TOPIC') or 'default'})"
