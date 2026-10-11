"""The everyday tools for a server that is already set up: push, service, backup, public-url, doctor."""
import asyncio
import json
import os
import sqlite3
import stat
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from gaca_api import cli, ops
from gaca_api.config import settings
from gaca_api.db import client
from gaca_api.installer import push as push_mod
from gaca_api.installer.run import Say
from gaca_api.installer.shell import Shell
from gaca_api.provisioning import envfile, hermes_connect, skill_sync
from gaca_api.services import pairing

PEM = "-----BEGIN PRIVATE KEY-----\nMIGTAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBHkwdwIBAQQg\n-----END PRIVATE KEY-----\n"
SKILLS = Path(__file__).resolve().parents[2] / "skills"


class FakeShell(Shell):
    def __init__(self, *, euid=1000, tty=False, sudo_n=True, unit_active="active", restart_ok=True, api=200, public=200,
                 hermes="v0.21.5", gateway=200, linger="yes"):
        self._euid, self._tty, self.sudo_n, self.unit_active, self.restart_ok = euid, tty, sudo_n, unit_active, restart_ok
        self.api, self.public, self.hermes, self.gateway, self.linger = api, public, hermes, gateway, linger
        self.calls, self.sleeps = [], 0

    def run(self, argv, *, timeout=120, env=None, inherit=False):
        self.calls.append((list(argv), inherit))
        rc, out = 0, ""
        if argv[:3] == ["sudo", "-n", "systemctl"] or (argv[:1] == ["sudo"] and argv[1:2] == ["systemctl"]):
            rc = 0 if (self.sudo_n or inherit) and self.restart_ok else 1
        elif argv[:2] == ["loginctl", "show-user"]:
            out = f"Linger={self.linger}\n"
        elif "is-active" in argv:
            out = self.unit_active + "\n"
            rc = 0 if self.unit_active == "active" else 3
        elif "restart" in argv:
            rc = 0 if self.restart_ok else 1
        elif argv[1:2] == ["--version"] and argv[0].endswith("hermes"):
            out, rc = (f"Hermes Agent {self.hermes}\n", 0) if self.hermes else ("", 1)
        return subprocess.CompletedProcess(argv, rc, out, "boom" if rc else "")

    def which(self, name):
        return f"/usr/bin/{name}" if name != "hermes" or self.hermes else None

    def is_tty(self): return self._tty
    def euid(self): return self._euid
    def sleep(self, seconds): self.sleeps += 1

    def http_get(self, url, timeout=8):
        if url.startswith("http://127.0.0.1:41700"):
            return (200, "{}") if self.api == 200 else (0, "")
        if url.endswith("/health") and ":8642" in url:
            return (self.gateway, "ok") if self.gateway else (0, "")
        if url.startswith("https://"):
            return self.public, ""
        return 0, ""

    def called(self, *prefix):
        return [c for c, _ in self.calls if c[:len(prefix)] == list(prefix)]


@pytest.fixture
def box(tmp_path, monkeypatch):
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    (home / ".config" / "gaca").mkdir(parents=True)
    repo.mkdir()
    (repo / ".env").write_text("GACA_DB_PATH=/x/gaca.db\nOTHER=keep\n")
    monkeypatch.setattr(settings, "api_port", 41700)
    monkeypatch.setattr(settings, "public_url", "")
    for name in ("fcm_credentials_path", "apns_key_path", "apns_key_id", "apns_team_id"):
        monkeypatch.setattr(settings, name, None)
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "gaca.db"))
    monkeypatch.setattr(settings, "hermes_home", str(home / ".hermes"))
    monkeypatch.setattr(ops, "SYSTEM_UNIT_DIR", tmp_path / "etc-systemd")
    return ops.Paths(repo=repo, config=home / ".config" / "gaca", home=home)


def say_into(lines):
    return Say(lines.append)


def user_unit(box, name="gaca-api"):
    path = box.home / ".config" / "systemd" / "user" / f"{name}.service"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[Service]\n")


def system_unit(box, name="gaca-api"):
    ops.SYSTEM_UNIT_DIR.mkdir(parents=True, exist_ok=True)
    (ops.SYSTEM_UNIT_DIR / f"{name}.service").write_text("[Service]\n")


def fcm_key(tmp_path, project="my-gaca"):
    path = tmp_path / "firebase.json"
    path.write_text(json.dumps({"type": "service_account", "project_id": project, "client_email": "a@b.c", "private_key": PEM}))
    return path


def p8(tmp_path):
    path = tmp_path / "AuthKey.p8"
    path.write_text(PEM)
    return path


def env(box):
    return envfile.read_text(box.env_file)


# ── envfile helpers ──────────────────────────────────────────────────

def test_remove_env_drops_only_the_named_keys_and_keeps_everything_else(tmp_path):
    path = tmp_path / ".env"
    path.write_text("A=1\n# a comment\nB=2\nA=again\n\nC=3\n")
    assert envfile.remove_env(path, ["A", "ZZ"]) == ["A"]
    assert path.read_text() == "# a comment\nB=2\n\nC=3\n" and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert envfile.remove_env(path, ["NOPE"]) == []


def test_read_value(tmp_path):
    path = tmp_path / ".env"
    path.write_text("A=1\nB='two'\nC=\n")
    assert envfile.read_value(path, "A") == "1" and envfile.read_value(path, "B") == "two"
    assert envfile.read_value(path, "C") == "" and envfile.read_value(path, "Z") is None


# ── skills: a dry run reports and writes nothing ─────────────────────

def test_a_skills_dry_run_reports_without_writing(tmp_path):
    skills = tmp_path / "skills"
    skills.mkdir()
    plan = skill_sync.sync(SKILLS, skills, dry_run=True)
    assert plan.written and not (skills / "gaca").exists()
    skill_sync.sync(SKILLS, skills)
    assert not skill_sync.sync(SKILLS, skills, dry_run=True).changed
    (skills / "gaca" / "gaca-lists" / "SKILL.md").write_text("edited")
    assert skill_sync.sync(SKILLS, skills, dry_run=True).left_alone == ["gaca-lists/SKILL.md"]
    assert (skills / "gaca" / "gaca-lists" / "SKILL.md").read_text() == "edited"
    assert not skill_sync.sync(SKILLS, tmp_path / "absent", dry_run=True).left_alone
    assert not (tmp_path / "absent").exists()


# ── push, after setup ────────────────────────────────────────────────

def test_push_status_says_what_is_on_and_what_is_broken(box, tmp_path):
    lines = []
    ops.push_status(say_into(lines), box)
    assert any("Android: off" in l for l in lines) and any("iPhone: off" in l for l in lines)
    ops.push_android(FakeShell(), Say(lambda *_: None), "gaca-api", box, fcm_key(tmp_path), restart=False)
    lines.clear()
    ops.push_status(say_into(lines), box)
    assert any("Android: on (Firebase project my-gaca)" in l for l in lines)
    (box.secrets / push_mod.FCM_FILE).write_text("not json")
    lines.clear()
    ops.push_status(say_into(lines), box)
    assert any("Android: BROKEN" in l for l in lines)


def test_push_android_after_setup_installs_the_key_and_says_to_restart(box, tmp_path):
    lines = []
    assert ops.push_android(FakeShell(), say_into(lines), "gaca-api", box, fcm_key(tmp_path), restart=False) == 0
    secret = box.secrets / push_mod.FCM_FILE
    assert envfile.read_value(box.env_file, "GACA_FCM_CREDENTIALS_PATH") == str(secret)
    assert stat.S_IMODE(secret.stat().st_mode) == 0o600 and "OTHER=keep" in env(box)
    assert any("Android push is set up (Firebase project my-gaca)" in l for l in lines) and any("gaca-api service restart" in l for l in lines)
    assert not any("MIGTAgEAMBMG" in l for l in lines)


def test_push_android_can_replace_an_existing_key(box, tmp_path):
    ops.push_android(FakeShell(), Say(lambda *_: None), "gaca-api", box, fcm_key(tmp_path, "first"), restart=False)
    ops.push_android(FakeShell(), Say(lambda *_: None), "gaca-api", box, fcm_key(tmp_path, "second"), restart=False)
    assert "second" in (box.secrets / push_mod.FCM_FILE).read_text() and env(box).count("GACA_FCM_CREDENTIALS_PATH") == 1


def test_push_with_restart_restarts_the_service(box, tmp_path):
    user_unit(box)
    shell = FakeShell()
    assert ops.push_android(shell, Say(lambda *_: None), "gaca-api", box, fcm_key(tmp_path), restart=True) == 0
    assert shell.called("systemctl", "--user", "restart", "gaca-api.service")


def test_push_android_needs_a_key_when_there_is_no_terminal(box):
    lines = []
    assert ops.push_android(FakeShell(tty=False), say_into(lines), "gaca-api", box, None, restart=False) == 2
    assert any("--key FILE" in l for l in lines) and not box.secrets.exists()


def test_push_android_asks_at_a_terminal_and_a_bad_file_is_refused(box, tmp_path, monkeypatch):
    answers = iter([str(tmp_path / "missing.json"), str(fcm_key(tmp_path))])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    lines = []
    assert ops.push_android(FakeShell(tty=True), say_into(lines), "gaca-api", box, None, restart=False) == 0
    assert any("cannot read" in l for l in lines) and any("same Firebase project" in l for l in lines)
    junk = tmp_path / "junk.json"
    junk.write_text("{}")
    lines.clear()
    assert ops.push_android(FakeShell(), say_into(lines), "gaca-api", box, junk, restart=False) == 1
    assert any("service_account" in l for l in lines)


def test_push_android_can_be_cancelled_with_a_blank_answer(box, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    assert ops.push_android(FakeShell(tty=True), Say(lambda *_: None), "gaca-api", box, None, restart=False) == 1
    assert "GACA_FCM" not in env(box)


def test_push_ios_after_setup_from_flags(box, tmp_path):
    lines = []
    assert ops.push_ios(FakeShell(), say_into(lines), "gaca-api", box, p8(tmp_path), "abc123defg", "TEAM123456", "com.example.gaca", restart=False) == 0
    values = push_mod.read_env(box.env_file)
    assert values["GACA_APNS_KEY_ID"] == "ABC123DEFG" and values["GACA_APNS_TOPIC"] == "com.example.gaca"
    assert values["GACA_APNS_KEY_PATH"] == str(box.secrets / push_mod.APNS_FILE) and "OTHER=keep" in env(box)


def test_push_ios_without_everything_and_without_a_terminal_says_what_to_pass(box, tmp_path):
    lines = []
    assert ops.push_ios(FakeShell(tty=False), say_into(lines), "gaca-api", box, p8(tmp_path), None, None, None, restart=False) == 2
    assert any("--key-id" in l and "--bundle-id" in l for l in lines) and "GACA_APNS" not in env(box)


def test_push_ios_asks_only_for_what_is_missing(box, tmp_path, monkeypatch):
    asked = []
    answers = iter(["abc123defg", "TEAM123456"])
    monkeypatch.setattr("builtins.input", lambda prompt="": asked.append(prompt) or next(answers))
    assert ops.push_ios(FakeShell(tty=True), Say(lambda *_: None), "gaca-api", box, p8(tmp_path), None, None, "com.example.gaca", restart=False) == 0
    assert [a.split(" ")[0] for a in asked] == ["Key", "Team"]
    assert push_mod.read_env(box.env_file)["GACA_APNS_TEAM_ID"] == "TEAM123456"


def test_push_remove_turns_one_platform_off_and_deletes_its_key(box, tmp_path):
    quiet = Say(lambda *_: None)
    ops.push_android(FakeShell(), quiet, "gaca-api", box, fcm_key(tmp_path), restart=False)
    ops.push_ios(FakeShell(), quiet, "gaca-api", box, p8(tmp_path), "ABC123DEFG", "TEAM123456", "com.example.gaca", restart=False)
    lines = []
    assert ops.push_remove(FakeShell(), say_into(lines), "gaca-api", box, "android", restart=False) == 0
    assert "GACA_FCM" not in env(box) and not (box.secrets / push_mod.FCM_FILE).exists()
    assert (box.secrets / push_mod.APNS_FILE).exists() and "GACA_APNS_KEY_ID" in env(box) and "OTHER=keep" in env(box)
    ops.push_remove(FakeShell(), quiet, "gaca-api", box, "ios", restart=False)
    assert "GACA_APNS" not in env(box) and not box.secrets.joinpath(push_mod.APNS_FILE).exists()
    lines.clear()
    ops.push_remove(FakeShell(), say_into(lines), "gaca-api", box, "ios", restart=False)
    assert any("was not set up" in l for l in lines)


# ── the public address ───────────────────────────────────────────────

def test_public_url_shows_sets_and_validates(box):
    lines = []
    assert ops.public_url(say_into(lines), box, None) == 0 and any("no public address saved" in l for l in lines)
    assert ops.public_url(say_into(lines), box, "https://Gaca.Example.com/") == 0
    assert envfile.read_value(box.env_file, "GACA_PUBLIC_URL") == "https://Gaca.Example.com"
    lines.clear()
    ops.public_url(say_into(lines), box, None)
    assert any("https://Gaca.Example.com" in l for l in lines)
    for bad in ("http://gaca.example.com", "https://gaca.example.com/api", "https://u:p@gaca.example.com", "gaca.example.com",
                "https://gaca.example.com/?x=1", ""):
        assert ops.public_url(say_into([]), box, bad) == 2, bad
    assert envfile.read_value(box.env_file, "GACA_PUBLIC_URL") == "https://Gaca.Example.com"  # unchanged by the bad ones


# ── a safe copy of the database ──────────────────────────────────────

def make_db(path):
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE t (n INTEGER)")
    con.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(100)])
    con.commit()
    return con  # left open on purpose: a live server holds the file


def test_backup_copies_a_live_database_privately(box, tmp_path):
    db = tmp_path / "live.db"
    live = make_db(db)
    target = ops.backup_database(db, None, box.home, now=datetime(2026, 10, 11, 12, 0, 0))
    assert target == box.home / "gaca-backups" / "gaca-20261011-120000.db"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600 and stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    assert sqlite3.connect(target).execute("SELECT COUNT(*) FROM t").fetchone()[0] == 100
    live.execute("INSERT INTO t VALUES (1000)")
    live.commit()
    assert sqlite3.connect(target).execute("SELECT COUNT(*) FROM t").fetchone()[0] == 100  # a snapshot, not a link
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM t").fetchone()[0] == 101  # the original is untouched


def test_backup_refuses_to_overwrite_or_to_invent_a_database(box, tmp_path):
    db = tmp_path / "live.db"
    make_db(db)
    mine = tmp_path / "keep.db"
    mine.write_text("precious")
    with pytest.raises(FileExistsError):
        ops.backup_database(db, mine, box.home)
    assert mine.read_text() == "precious"
    with pytest.raises(FileNotFoundError):
        ops.backup_database(tmp_path / "nope.db", None, box.home)
    assert not (box.home / "gaca-backups").exists()


def test_backup_command_prints_where_it_went(box, tmp_path, monkeypatch, capsys):
    db = tmp_path / "gaca.db"
    make_db(db)
    monkeypatch.setattr(ops, "default_paths", lambda: box)
    assert cli.main(["backup", str(tmp_path / "out.db")]) == 0
    assert "Saved" in capsys.readouterr().out and (tmp_path / "out.db").exists()
    assert cli.main(["backup", str(tmp_path / "out.db")]) == 1  # now it exists
    assert "already exists" in capsys.readouterr().err


# ── the service ──────────────────────────────────────────────────────

def test_service_mode_is_found_from_the_unit_file(box):
    assert ops.service_mode("gaca-api", box) is None
    system_unit(box)
    assert ops.service_mode("gaca-api", box) == "system"
    user_unit(box)
    assert ops.service_mode("gaca-api", box) == "user"


def test_restart_a_user_service(box):
    user_unit(box)
    shell, lines = FakeShell(), []
    assert ops.service_restart(shell, say_into(lines), "gaca-api", box) == 0
    assert shell.called("systemctl", "--user", "restart", "gaca-api.service") and any("restarted and is answering" in l for l in lines)


def test_restart_a_system_service_uses_passwordless_sudo_when_there_is_no_terminal_prompt(box):
    system_unit(box)
    shell = FakeShell(sudo_n=True)
    assert ops.service_restart(shell, say_into([]), "gaca-api", box) == 0
    assert shell.called("sudo", "-n", "systemctl", "restart", "gaca-api.service")


def test_restart_a_system_service_asks_for_sudo_at_a_terminal(box):
    system_unit(box)
    shell = FakeShell(sudo_n=False, tty=True)
    assert ops.service_restart(shell, say_into([]), "gaca-api", box) == 0
    assert ("sudo systemctl restart gaca-api.service".split(), True) in [(c, i) for c, i in shell.calls]


def test_restart_without_root_or_a_terminal_prints_the_command(box):
    system_unit(box)
    lines = []
    assert ops.service_restart(FakeShell(sudo_n=False, tty=False), say_into(lines), "gaca-api", box) == 1
    assert any("sudo systemctl restart gaca-api.service" in l for l in lines)


def test_restart_as_root_needs_no_sudo(box):
    system_unit(box)
    shell = FakeShell(euid=0)
    assert ops.service_restart(shell, say_into([]), "gaca-api", box) == 0 and not shell.called("sudo")


def test_a_failed_restart_or_a_server_that_does_not_come_back_is_reported(box):
    user_unit(box)
    lines = []
    assert ops.service_restart(FakeShell(restart_ok=False), say_into(lines), "gaca-api", box) == 1 and any("restart failed" in l for l in lines)
    lines.clear()
    shell = FakeShell(api=0)
    assert ops.service_restart(shell, say_into(lines), "gaca-api", box) == 1
    assert any("not answering yet" in l for l in lines) and shell.sleeps >= 15  # it waited before giving up


def test_restart_with_no_service_says_so(box):
    lines = []
    assert ops.service_restart(FakeShell(), say_into(lines), "gaca-api", box) == 1 and any("no `gaca-api` service" in l for l in lines)


def test_service_status(box):
    user_unit(box)
    lines = []
    assert ops.service_status(FakeShell(), say_into(lines), "gaca-api", box) == 0 and any("active; answering" in l for l in lines)
    lines.clear()
    assert ops.service_status(FakeShell(unit_active="failed", api=0), say_into(lines), "gaca-api", box) == 1
    assert any("failed; NOT answering" in l for l in lines)


def test_service_logs_run_journalctl_for_the_right_kind_of_service(box):
    user_unit(box)
    shell = FakeShell()
    ops.service_logs(shell, "gaca-api", box, follow=False, lines=50)
    assert shell.called("journalctl", "--user", "-u", "gaca-api.service", "-n", "50", "--no-pager")
    shell = FakeShell()
    ops.service_logs(shell, "gaca-api", box, follow=True, lines=10)
    assert shell.called("journalctl", "--user")[0][-1] == "-f" and "--no-pager" not in shell.called("journalctl")[0]
    (box.home / ".config" / "systemd" / "user" / "gaca-api.service").unlink()
    system_unit(box)
    shell = FakeShell()
    ops.service_logs(shell, "gaca-api", box, follow=False, lines=5)
    assert shell.called("journalctl", "-u", "gaca-api.service") and not shell.called("journalctl", "--user")


# ── doctor ───────────────────────────────────────────────────────────

def build_healthy_box(box, tmp_path, monkeypatch):
    """Service running, API up, a paired phone, Hermes ready with a working token and skills, push on."""
    user_unit(box)
    hermes_home = box.home / ".hermes"
    hermes_home.mkdir()
    (hermes_home / ".env").write_text("API_SERVER_ENABLED=true\nAPI_SERVER_KEY=" + "k" * 32 + "\n")
    monkeypatch.setattr(settings, "public_url", "https://gaca.example.com")
    fcm = box.secrets / push_mod.FCM_FILE
    push_mod.install_secret(fcm_key(tmp_path), box.secrets, push_mod.FCM_FILE)
    monkeypatch.setattr(settings, "fcm_credentials_path", str(fcm))
    box.env_file.write_text(f"GACA_FCM_CREDENTIALS_PATH={fcm}\n")

    async def go():
        await client.connect()
        try:
            code, _ = await pairing.issue_code()
            await pairing.pair(code, "Pixel")
            owner = await __import__("gaca_api.db.users", fromlist=["x"]).first()
            outcome = await hermes_connect.connect_profiles(hermes_home, "http://127.0.0.1:41700", owner)
            assert outcome[0].token_id
        finally:
            await client.close()
    asyncio.run(go())
    hermes_connect.install_skills(hermes_home)
    return hermes_home


def run_doctor(box, shell=None):
    lines = []
    code = ops.doctor(shell or FakeShell(), say_into(lines), "gaca-api", box)
    return code, "\n".join(lines)


def test_doctor_is_happy_when_everything_is_working(box, tmp_path, monkeypatch):
    build_healthy_box(box, tmp_path, monkeypatch)
    code, out = run_doctor(box)
    assert code == 0 and "Everything checks out" in out and "problem" not in out.replace("0 problem", "")
    for expected in ("service is running", "API answers on 127.0.0.1:41700", "public address https://gaca.example.com reaches",
                     "1 paired phone", "Hermes 0.21.5 is installed", "working GACA token", "skills are up to date", "Android push is on"):
        assert expected in out, expected


@pytest.mark.parametrize("shell_args, expected", [
    ({"unit_active": "failed"}, "service is failed"),
    ({"api": 0}, "does not answer on 127.0.0.1:41700"),
    ({"public": 502}, "does not reach this server (got 502)"),
    ({"hermes": None}, "Hermes is not installed"),
    ({"gateway": 0}, "not answering on 127.0.0.1:8642"),
])
def test_doctor_finds_what_is_wrong(box, tmp_path, monkeypatch, shell_args, expected):
    build_healthy_box(box, tmp_path, monkeypatch)
    code, out = run_doctor(box, FakeShell(**shell_args))
    assert code == 1 and expected in out and "problem(s) found" in out


def test_doctor_notices_a_revoked_hermes_token(box, tmp_path, monkeypatch):
    home = build_healthy_box(box, tmp_path, monkeypatch)

    async def revoke_all():
        await client.connect()
        try:
            from gaca_api.db import users as users_db
            from gaca_api.services import tokens as token_service
            owner = await users_db.first()
            for t in await token_service.list_tokens(owner):
                await token_service.revoke(owner, t.id)
        finally:
            await client.close()
    asyncio.run(revoke_all())
    code, out = run_doctor(box)
    assert code == 1 and "token is rejected" in out and "hermes connect --rotate" in out


def test_doctor_notices_a_missing_or_old_style_token(box, tmp_path, monkeypatch):
    home = build_healthy_box(box, tmp_path, monkeypatch)
    envfile.remove_env(home / ".env", ["GACA_API_TOKEN"])
    code, out = run_doctor(box)
    assert code == 1 and "has no GACA token" in out
    envfile.upsert_env(home / ".env", {"GACA_API_TOKEN": "old-style-jwt"})
    code, out = run_doctor(box)
    assert code == 1 and "old-style token" in out


def test_doctor_notices_skills_that_are_missing_and_tolerates_edited_ones(box, tmp_path, monkeypatch):
    home = build_healthy_box(box, tmp_path, monkeypatch)
    (home / "skills" / "gaca" / "gaca-lists" / "SKILL.md").write_text("an agent edited this")
    code, out = run_doctor(box)
    assert code == 0 and "were edited by you or an agent and are kept" in out
    (home / "skills" / "gaca" / "gaca-alerts" / "SKILL.md").unlink()
    code, out = run_doctor(box)
    assert code == 1 and "missing or out of date" in out and "hermes connect" in out


def test_doctor_flags_a_broken_push_key(box, tmp_path, monkeypatch):
    build_healthy_box(box, tmp_path, monkeypatch)
    (box.secrets / push_mod.FCM_FILE).write_text("garbage")
    code, out = run_doctor(box)
    assert code == 1 and "Android push: BROKEN" in out and "gaca-api push android" in out


def test_doctor_without_a_database_or_a_service_says_so(box):
    code, out = run_doctor(box)
    assert code == 1
    for expected in ("no `gaca-api` service is installed", "no database at", "no public address saved"):
        assert expected in out


def test_doctor_runs_through_the_cli(box, monkeypatch, capsys):
    monkeypatch.setattr(ops, "default_paths", lambda: box)
    assert cli.main(["doctor"]) == 1
    assert "problem(s) found" in capsys.readouterr().out


# ── the CLI is wired to all of it ────────────────────────────────────

def test_the_cli_commands_reach_the_tools(box, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ops, "default_paths", lambda: box)
    assert cli.main(["public-url", "https://gaca.example.com"]) == 0
    assert cli.main(["push", "status"]) == 0
    assert cli.main(["push", "android", "--key", str(fcm_key(tmp_path))]) == 0
    assert cli.main(["push", "ios", "--key", str(p8(tmp_path)), "--key-id", "ABC123DEFG", "--team-id", "TEAM123456", "--bundle-id", "com.example.gaca"]) == 0
    assert cli.main(["push", "remove", "android"]) == 0
    assert cli.main(["service", "status"]) == 1  # no service installed in this temp home
    out = capsys.readouterr().out
    assert "saved https://gaca.example.com" in out and "iPhone push is set up" in out and "Android push is off" in out
    with pytest.raises(SystemExit):
        cli.main(["push", "remove", "windows-phone"])


# ── gaps the mutation checks found ───────────────────────────────────

def test_remove_env_matches_whole_key_names_only(tmp_path):
    path = tmp_path / ".env"
    path.write_text("A=1\nAB=2\nXA=3\nGACA_FCM_CREDENTIALS_PATH_BACKUP=keep\nGACA_FCM_CREDENTIALS_PATH=gone\n")
    assert envfile.remove_env(path, ["A", "GACA_FCM_CREDENTIALS_PATH"]) == ["A", "GACA_FCM_CREDENTIALS_PATH"]
    assert path.read_text() == "AB=2\nXA=3\nGACA_FCM_CREDENTIALS_PATH_BACKUP=keep\n"


def test_a_dry_run_changes_nothing_even_when_the_skills_are_partly_there(tmp_path):
    skills = tmp_path / "skills"
    skills.mkdir()
    skill_sync.sync(SKILLS, skills)
    manifest = skills / "gaca" / skill_sync.MANIFEST
    victim = skills / "gaca" / "gaca-alerts" / "SKILL.md"
    victim.unlink()
    before = manifest.read_bytes(), manifest.stat().st_mtime_ns
    plan = skill_sync.sync(SKILLS, skills, dry_run=True)
    assert plan.written == ["gaca-alerts/SKILL.md"] and not victim.exists()
    assert (manifest.read_bytes(), manifest.stat().st_mtime_ns) == before
    # an out-of-date file and a dropped one are reported but not touched either
    stale = skills / "gaca" / "gaca-lists" / "SKILL.md"
    manifest_data = json.loads(manifest.read_text())
    stale.write_text("old")
    manifest_data["files"]["gaca-lists/SKILL.md"] = skill_sync._sha(b"old")
    (skills / "gaca" / "gone.md").write_text("x")
    manifest_data["files"]["gone.md"] = skill_sync._sha(b"x")
    manifest.write_text(json.dumps(manifest_data))
    plan = skill_sync.sync(SKILLS, skills, dry_run=True)
    assert "gaca-lists/SKILL.md" in plan.updated and plan.removed == ["gone.md"]
    assert stale.read_text() == "old" and (skills / "gaca" / "gone.md").exists()


def test_backup_leaves_the_live_database_and_its_wal_untouched(box, tmp_path):
    db = tmp_path / "live.db"
    live = make_db(db)  # held open, as the running server would hold it
    live.execute("INSERT INTO t VALUES (7)")
    live.commit()
    wal = Path(str(db) + "-wal")
    before = (db.read_bytes(), wal.read_bytes())
    target = ops.backup_database(db, tmp_path / "copy.db", box.home)
    assert sqlite3.connect(target).execute("SELECT COUNT(*) FROM t").fetchone()[0] == 101  # includes what is only in the WAL
    assert (db.read_bytes(), wal.read_bytes()) == before
    live.close()


def test_doctor_notes_when_lingering_is_off_but_does_not_fail_for_it(box, tmp_path, monkeypatch):
    build_healthy_box(box, tmp_path, monkeypatch)
    monkeypatch.setenv("USER", "ronna")
    code, out = run_doctor(box, FakeShell(linger="no"))
    assert code == 0 and "lingering is off" in out and "sudo loginctl enable-linger ronna" in out
    code, out = run_doctor(box, FakeShell(linger="yes"))
    assert code == 0 and "lingering is off" not in out
