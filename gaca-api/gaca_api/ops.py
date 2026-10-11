"""Everyday tools for a server that is already set up: `gaca-api push | doctor | service | backup | public-url`.

Setup (./install.sh) is a one-off; these are what you reach for afterwards: add push later, check that
everything still works, restart or read the logs of the service without remembering systemctl flags,
take a safe copy of the database, change the address phones are told to use.

Each tool takes its paths and its `Shell` explicitly so it can be tested without touching the machine.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from .config import settings
from .installer import hermes as hermes_mod
from .installer import push as push_mod
from .installer.run import SYSTEM_UNIT_DIR, Say, _ask, _ask_valid, _hermes_bin
from .installer.shell import Shell, user_systemd_env
from .provisioning import envfile, hermes_connect, skill_sync


@dataclass
class Paths:
    repo: Path
    config: Path
    home: Path

    @property
    def env_file(self) -> Path:
        return self.repo / ".env"

    @property
    def secrets(self) -> Path:
        return push_mod.secrets_dir(self.config)


def default_paths() -> Paths:
    home = Path.home()
    return Paths(repo=Path(__file__).resolve().parents[2], config=home / ".config" / "gaca", home=home)


# ── the service ──────────────────────────────────────────────────────

def service_mode(name: str, paths: Paths) -> str | None:
    if (paths.home / ".config" / "systemd" / "user" / f"{name}.service").exists():
        return "user"
    if (SYSTEM_UNIT_DIR / f"{name}.service").exists():
        return "system"
    return None


def _systemctl(mode: str, *args: str) -> tuple[list[str], dict | None]:
    if mode == "user":
        return ["systemctl", "--user", *args], user_systemd_env()
    return ["systemctl", *args], None


def _healthy(shell: Shell, wait: float = 20) -> bool:
    waited = 0.0
    while True:
        code, _ = shell.http_get(f"http://127.0.0.1:{settings.api_port}/api/health")
        if code == 200:
            return True
        if waited >= wait:
            return False
        shell.sleep(1)
        waited += 1


def service_status(shell: Shell, say: Say, name: str, paths: Paths) -> int:
    mode = service_mode(name, paths)
    if mode is None:
        say.warn(f"no `{name}` service is installed here (run ./install.sh)")
        return 1
    argv, env = _systemctl(mode, "is-active", f"{name}.service")
    state = (shell.run(argv, env=env).stdout or "").strip() or "unknown"
    healthy = _healthy(shell, wait=0)
    (say.ok if state == "active" and healthy else say.warn)(
        f"{name} ({mode} service): {state}; " + (f"answering on 127.0.0.1:{settings.api_port}" if healthy else "NOT answering")
    )
    return 0 if state == "active" and healthy else 1


def service_restart(shell: Shell, say: Say, name: str, paths: Paths) -> int:
    mode = service_mode(name, paths)
    if mode is None:
        say.warn(f"no `{name}` service is installed here (run ./install.sh)")
        return 1
    argv, env = _systemctl(mode, "restart", f"{name}.service")
    stderr = ""
    if mode == "user" or shell.euid() == 0:
        done = shell.run(argv, env=env)
        restarted, stderr = done.returncode == 0, done.stderr or ""
    else:
        quiet = shell.run(["sudo", "-n", *argv])
        if quiet.returncode == 0:
            restarted = True
        elif shell.is_tty():
            restarted = shell.run(["sudo", *argv], inherit=True).returncode == 0
        else:
            say.warn("restarting a system service needs root, and there is no terminal to ask for a password. Run:")
            say.note("    sudo " + " ".join(argv))
            return 1
    if not restarted:
        say.warn(f"restart failed: {stderr.strip()[-300:] or 'see `gaca-api service logs`'}")
        return 1
    if _healthy(shell):
        say.ok(f"{name} restarted and is answering")
        return 0
    say.warn(f"{name} restarted but is not answering yet; see `gaca-api service logs`")
    return 1


def service_logs(shell: Shell, name: str, paths: Paths, *, follow: bool, lines: int) -> int:
    mode = service_mode(name, paths)
    if mode is None:
        print(f"no `{name}` service is installed here (run ./install.sh)")
        return 1
    argv = ["journalctl", *(["--user"] if mode == "user" else []), "-u", f"{name}.service", "-n", str(lines), "--no-pager"]
    if follow:
        argv = [a for a in argv if a != "--no-pager"] + ["-f"]
    env = user_systemd_env() if mode == "user" else None
    return shell.run(argv, env=env, inherit=True, timeout=None if follow else 120).returncode


# ── push notifications, after setup ──────────────────────────────────

def push_status(say: Say, paths: Paths) -> int:
    env = push_mod.read_env(paths.env_file)
    for label, line in (("Android", push_mod.describe_android(env)), ("iPhone", push_mod.describe_ios(env))):
        (say.warn if line.startswith("BROKEN") else say.ok if line.startswith("on") else say.note)(f"{label}: {line}")
    say.note("Add or change one with `gaca-api push android` / `gaca-api push ios`; turn one off with `gaca-api push remove android|ios`.")
    return 0


def _after_push_change(shell: Shell, say: Say, name: str, paths: Paths, restart: bool) -> int:
    if restart:
        return service_restart(shell, say, name, paths)
    say.note("Restart the server for this to take effect:  gaca-api service restart   (or add --restart)")
    return 0


def push_android(shell: Shell, say: Say, name: str, paths: Paths, key: Path | None, *, restart: bool) -> int:
    if key is None:
        if not shell.is_tty():
            say.warn("give the key file with --key FILE (the Firebase service-account JSON)")
            return 2
        say.note("Firebase console > Project settings > Service accounts > Generate new private key.")
        say.note("It must be the same Firebase project your app was built with (its google-services.json).")
        answer = _ask_valid("Path to the downloaded key file (blank to cancel): ", lambda a: (push_mod.check_fcm(Path(a)), a)[1], say)
        if not answer:
            say.note("cancelled")
            return 1
        key = Path(answer)
    try:
        project = push_mod.setup_android(paths.env_file, paths.secrets, key)
    except push_mod.PushSetupError as error:
        say.warn(str(error))
        return 1
    say.ok(f"Android push is set up (Firebase project {project})")
    return _after_push_change(shell, say, name, paths, restart)


def push_ios(shell: Shell, say: Say, name: str, paths: Paths, key: Path | None, key_id: str | None,
             team_id: str | None, bundle_id: str | None, *, restart: bool) -> int:
    if not all((key, key_id, team_id, bundle_id)):
        if not shell.is_tty():
            say.warn("give --key FILE --key-id ID --team-id ID --bundle-id BUNDLE_ID (or run this at a terminal to be asked)")
            return 2
        say.note("From the Apple Developer portal: a Key with the Apple Push Notifications service enabled (a .p8 file),")
        say.note("its Key ID, your Team ID, and the bundle id of the app you built.")
        key = key or _path(_ask_valid("Path to the .p8 key file (blank to cancel): ", lambda a: (push_mod.check_apns_key(Path(a)), a)[1], say))
        key_id = key and (key_id or _ask_valid("Key ID (10 characters): ", lambda a: push_mod.check_apple_id(a, "key id"), say))
        team_id = key_id and (team_id or _ask_valid("Team ID (10 characters): ", lambda a: push_mod.check_apple_id(a, "team id"), say))
        bundle_id = team_id and (bundle_id or _ask_valid("The app's bundle id (like com.example.gaca): ", push_mod.check_bundle_id, say))
        if not bundle_id:
            say.note("cancelled")
            return 1
    try:
        push_mod.setup_ios(paths.env_file, paths.secrets, key, key_id, team_id, bundle_id)
    except push_mod.PushSetupError as error:
        say.warn(str(error))
        return 1
    say.ok("iPhone push is set up")
    return _after_push_change(shell, say, name, paths, restart)


def _path(value):
    return Path(value) if value else None


def push_remove(shell: Shell, say: Say, name: str, paths: Paths, platform: str, *, restart: bool) -> int:
    was_on = (push_mod.remove_android if platform == "android" else push_mod.remove_ios)(paths.env_file, paths.secrets)
    label = "Android" if platform == "android" else "iPhone"
    if not was_on:
        say.note(f"{label} push was not set up")
        return 0
    say.ok(f"{label} push is off, and the key file is deleted")
    return _after_push_change(shell, say, name, paths, restart)


# ── the public address ───────────────────────────────────────────────

def public_url(say: Say, paths: Paths, value: str | None) -> int:
    if value is None:
        current = envfile.read_value(paths.env_file, "GACA_PUBLIC_URL")
        say.note(f"phones are told to use: {current}" if current else "no public address saved: `gaca-api public-url https://gaca.example.com`")
        return 0
    parts = urlsplit(value.strip())
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment or parts.path not in ("", "/"):
        say.warn("give the address as https://host (no path, no credentials), e.g. https://gaca.example.com")
        return 2
    origin = f"https://{parts.netloc}"
    envfile.upsert_env(paths.env_file, {"GACA_PUBLIC_URL": origin})
    say.ok(f"saved {origin}: `gaca-api pair` now puts it in the QR code (no restart needed)")
    return 0


# ── a safe copy of the database ──────────────────────────────────────

def backup_database(db_path: Path, destination: Path | None, home: Path, now: datetime | None = None) -> Path:
    """Copy the live database (consistent even while the server is writing) to a private file."""
    db_path = Path(db_path).expanduser()
    if not db_path.is_file():
        raise FileNotFoundError(f"no database at {db_path}")
    if destination is None:
        stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
        destination = home / "gaca-backups" / f"gaca-{stamp}.db"
    destination = Path(destination).expanduser()
    if destination.exists():
        raise FileExistsError(f"{destination} already exists; choose another name")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.parent == home / "gaca-backups":
        os.chmod(destination.parent, 0o700)
    source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
        finally:
            target.close()
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    finally:
        source.close()
    os.chmod(destination, 0o600)
    return destination


# ── is everything working? ───────────────────────────────────────────

@dataclass
class Report:
    problems: int = 0
    lines: list[tuple[str, str, list[str]]] = field(default_factory=list)

    def ok(self, text: str) -> None:
        self.lines.append(("ok", text, []))

    def note(self, text: str, fix: list[str] | None = None) -> None:
        self.lines.append(("note", text, fix or []))

    def problem(self, text: str, fix: list[str] | None = None) -> None:
        self.problems += 1
        self.lines.append(("problem", text, fix or []))


def _check_database(report: Report, hermes_home: Path, source: Path) -> None:
    from .db import client
    from .db import users as users_db
    from .services import tokens as token_service

    async def go() -> None:
        await client.connect()
        try:
            owner = await users_db.first()
            if owner is None:
                report.note("no phone has paired yet", ["run `gaca-api pair` and scan the code in the app"])
            else:
                devices = await token_service.list_devices(owner)
                if devices:
                    report.ok(f"{len(devices)} paired phone(s)")
                else:
                    report.note("no phone is paired", ["run `gaca-api pair` and scan the code in the app"])
            for profile, env_path in hermes_connect.profile_env_paths(hermes_home).items():
                token = envfile.read_value(env_path, "GACA_API_TOKEN")
                if not token:
                    report.problem(f"Hermes profile {profile!r} has no GACA token", ["run `gaca-api hermes connect`"])
                elif not token.startswith("gaca_pat_"):
                    report.problem(f"Hermes profile {profile!r} has an old-style token", ["run `gaca-api hermes connect --rotate`"])
                elif await token_service.authenticate(token) is None:
                    report.problem(f"Hermes profile {profile!r}'s GACA token is rejected (revoked?)", ["run `gaca-api hermes connect --rotate`"])
                else:
                    report.ok(f"Hermes profile {profile!r} has a working GACA token")
        finally:
            await client.close()

    asyncio.run(go())


def doctor(shell: Shell, say: Say, name: str, paths: Paths) -> int:
    report = Report()
    home = Path(settings.hermes_home).expanduser()

    mode = service_mode(name, paths)
    if mode is None:
        report.problem(f"no `{name}` service is installed here", ["run ./install.sh"])
    else:
        argv, env = _systemctl(mode, "is-active", f"{name}.service")
        state = (shell.run(argv, env=env).stdout or "").strip() or "unknown"
        if state == "active":
            report.ok(f"the {mode} service is running")
        else:
            report.problem(f"the {mode} service is {state}", ["gaca-api service restart", "gaca-api service logs"])
    if _healthy(shell, wait=0):
        report.ok(f"the API answers on 127.0.0.1:{settings.api_port}")
    else:
        report.problem(f"the API does not answer on 127.0.0.1:{settings.api_port}", ["gaca-api service logs"])

    if settings.public_url:
        code, _ = shell.http_get(settings.public_url.rstrip("/") + "/api/health")
        if code == 200:
            report.ok(f"the public address {settings.public_url} reaches this server")
        else:
            report.problem(f"the public address {settings.public_url} does not reach this server (got {code or 'no answer'})",
                           ["check that the tunnel is running: docker ps", "and that its hostname points at HTTP localhost:" + str(settings.api_port)])
    else:
        report.note("no public address saved, so `gaca-api pair` cannot put one in the QR code",
                    ["gaca-api public-url https://gaca.example.com"])

    db = Path(settings.db_path).expanduser()
    if not db.is_file():
        report.problem(f"no database at {db}", ["start the server once, or set GACA_DB_PATH"])
    else:
        _check_database(report, home, db)

    status = hermes_mod.inspect(shell, home, _hermes_bin(shell))
    found = hermes_mod.problems(status, home)
    if not found:
        report.ok(f"Hermes {'.'.join(map(str, status.version))} is installed and its gateway answers on 127.0.0.1:{status.port}")
    for problem in found:
        report.problem(problem.what, problem.fix)

    for profile, env_path in hermes_connect.profile_env_paths(home).items():
        try:
            plan = skill_sync.sync(skill_sync.default_source(), env_path.parent / "skills", dry_run=True)
        except skill_sync.SkillSyncError as error:
            report.problem(f"GACA skills in Hermes profile {profile!r}: {error}")
            continue
        if plan.written or plan.updated or plan.removed:
            report.problem(f"GACA skills in Hermes profile {profile!r} are missing or out of date", ["run `gaca-api hermes connect`"])
        elif plan.left_alone:
            report.note(f"GACA skills in {profile!r}: {len(plan.left_alone)} file(s) were edited by you or an agent and are kept")
        else:
            report.ok(f"GACA skills are up to date in Hermes profile {profile!r}")

    env = push_mod.read_env(paths.env_file)
    for label, line in (("Android", push_mod.describe_android(env)), ("iPhone", push_mod.describe_ios(env))):
        if line.startswith("BROKEN"):
            report.problem(f"{label} push: {line}", [f"fix or remove it: gaca-api push {'android' if label == 'Android' else 'ios'}"])
        elif line.startswith("on"):
            report.ok(f"{label} push is {line}")
        else:
            report.note(f"{label} push is off", [f"gaca-api push {'android' if label == 'Android' else 'ios'}"])

    for level, text, fix in report.lines:
        {"ok": say.ok, "note": say.note, "problem": say.warn}[level](text if level != "note" else f"· {text}")
        for line in fix:
            say.note(f"    {line}" if not line.startswith("    ") else line)
    if report.problems:
        say.out(f"\n{report.problems} problem(s) found.")
        return 1
    say.out("\nEverything checks out.")
    return 0
