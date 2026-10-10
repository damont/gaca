"""The installer: secrets, privilege handling, tunnel files, and the whole flow with a fake machine.

Nothing here touches the real system: a FakeShell records every command and answers from a script,
and everything lives in tmp dirs."""
import asyncio
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from gaca_api.config import DEFAULT_JWT_SECRET, settings
from gaca_api.db import client
from gaca_api.installer import run as run_mod
from gaca_api.installer import tunnel as tunnel_mod
from gaca_api.installer.envconfig import ensure_env
from gaca_api.installer.privilege import NeedsRoot, run_as_root
from gaca_api.installer.run import Options, Say, choose_service, print_setup_info
from gaca_api.installer.shell import Shell
from gaca_api.installer.units import render, system_install_script
from gaca_api.services import auth as auth_service

REPO = Path(__file__).resolve().parents[2]


class FakeShell(Shell):
    def __init__(self, *, euid=1000, tty=False, sudo_n=False, docker=True, health=True, claimed=lambda: False,
                 logs="", public_status=200, fail=()):
        self._euid, self._tty, self._sudo_n, self._docker = euid, tty, sudo_n, docker
        self.health, self.claimed, self.logs, self.public_status, self.fail = health, claimed, logs, public_status, fail
        self.calls, self.sleeps = [], 0
        self.on_sleep = lambda: None

    def run(self, argv, *, timeout=120, env=None, inherit=False):
        self.calls.append((list(argv), inherit))
        rc, out = 0, ""
        if argv[:3] == ["sudo", "-n", "true"]:
            rc = 0 if self._sudo_n else 1
        elif argv[:2] == ["docker", "info"]:
            rc = 0 if self._docker else 1
        elif argv[:2] == ["docker", "logs"]:
            out = self.logs
        elif any(f in " ".join(argv) for f in self.fail):
            rc = 1
        return subprocess.CompletedProcess(argv, rc, out, "boom" if rc else "")

    def which(self, name):
        return f"/usr/bin/{name}" if (name != "docker" or self._docker) and name != "hermes" else None

    def is_tty(self): return self._tty
    def euid(self): return self._euid

    def sleep(self, seconds):
        self.sleeps += 1
        self.on_sleep()

    def http_get(self, url, timeout=8):
        if url.endswith("/api/health") and url.startswith("http://127.0.0.1"):
            return (200, '{"service":"gaca-api"}') if self.health else (0, "")
        if url.endswith("/api/setup"):
            return 200, json.dumps({"claimed": bool(self.claimed())})
        if url.endswith("/api/health"):
            return self.public_status, '{"service":"gaca-api"}' if self.public_status == 200 else ""
        return 0, ""

    def called(self, *prefix):
        return [c for c, _ in self.calls if c[:len(prefix)] == list(prefix)]


# ── env ──────────────────────────────────────────────────────────────

def env_kwargs(tmp_path, **over):
    kwargs = dict(data_dir=tmp_path / "data", port=41700, hermes_home=tmp_path / "hermes", hermes_bin=None)
    kwargs.update(over)
    return kwargs


def read_env(path):
    return dict(line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)


def test_a_fresh_env_gets_everything_and_a_strong_secret(tmp_path):
    path = tmp_path / ".env"
    notes = ensure_env(path, **env_kwargs(tmp_path, hermes_bin=None))
    values = read_env(path)
    assert len(values["GACA_JWT_SECRET"]) >= 43 and " " not in values["GACA_JWT_SECRET"]
    assert values["GACA_DB_PATH"] == str(tmp_path / "data" / "gaca.db")
    assert values["GACA_AGENT_ACCESS"] == "local" and values["GACA_AGENT_ENGINE"] == "hermes"
    assert values["GACA_API_HOST"] == "127.0.0.1" and "GACA_HERMES_BIN" not in values
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and any("generated" in n for n in notes)


def test_rerunning_keeps_the_secret_and_everything_else(tmp_path):
    path = tmp_path / ".env"
    ensure_env(path, **env_kwargs(tmp_path))
    path.write_text(path.read_text() + "MY_OWN_SETTING=keep-me\nGACA_API_PORT=9999\n")
    before = read_env(path)
    notes = ensure_env(path, **env_kwargs(tmp_path))
    after = read_env(path)
    assert after == before  # nothing changed...
    assert after["GACA_API_PORT"] == "9999" and after["MY_OWN_SETTING"] == "keep-me"  # ...including the operator's own edits
    assert after["GACA_JWT_SECRET"] == before["GACA_JWT_SECRET"] and notes == [".env already complete"]


@pytest.mark.parametrize("weak", [DEFAULT_JWT_SECRET, "short"])
def test_a_placeholder_or_short_secret_is_replaced_but_a_strong_one_never_is(tmp_path, weak):
    path = tmp_path / ".env"
    path.write_text(f"GACA_JWT_SECRET={weak}\n")
    notes = ensure_env(path, **env_kwargs(tmp_path))
    assert read_env(path)["GACA_JWT_SECRET"] != weak and any("weak" in n for n in notes)
    strong = read_env(path)["GACA_JWT_SECRET"]
    ensure_env(path, **env_kwargs(tmp_path))
    assert read_env(path)["GACA_JWT_SECRET"] == strong


def test_hermes_bin_is_recorded_when_found(tmp_path):
    path = tmp_path / ".env"
    ensure_env(path, **env_kwargs(tmp_path, hermes_bin="/home/u/.local/bin/hermes"))
    assert read_env(path)["GACA_HERMES_BIN"] == "/home/u/.local/bin/hermes"


# ── units ────────────────────────────────────────────────────────────

VALUES = dict(USER="alice", HOME="/home/alice", REPO="/home/alice/gaca", DATA_DIR="/home/alice/gaca-data",
              HERMES_HOME="/home/alice/.hermes", CONFIG_DIR="/home/alice/.config/gaca")


def test_the_system_unit_renders_with_the_sandbox_and_optional_paths():
    unit = render((REPO / "deploy" / "gaca-api.service").read_text(), **VALUES)
    assert "@" not in "".join(l for l in unit.splitlines() if not l.startswith("#"))
    assert "User=alice" in unit and "ExecStart=/home/alice/gaca/gaca-api/.venv/bin/gaca-api" in unit
    assert "ProtectHome=tmpfs" in unit and "ProtectSystem=strict" in unit and "NoNewPrivileges=yes" in unit
    assert "BindReadOnlyPaths=/home/alice/gaca -/home/alice/.hermes -/home/alice/.local -/home/alice/.config/gaca/secrets" in unit
    assert "BindPaths=/home/alice/gaca-data -/home/alice/.hermes/profiles" in unit  # optional paths won't break a Hermes-less box


def test_the_user_unit_is_honest_about_having_no_sandbox():
    unit = render((REPO / "deploy" / "gaca-api.user.service").read_text(), **VALUES)
    assert "ProtectHome" not in "".join(l for l in unit.splitlines() if not l.startswith("#"))
    assert "NO-SANDBOX" in unit and "WantedBy=default.target" in unit and "User=" not in unit


def test_unfilled_placeholders_are_an_error():
    with pytest.raises(ValueError, match="@NOPE@"):
        render("a @NOPE@ b @USER@", USER="x")


def test_the_root_script_installs_and_restarts_the_unit():
    script = system_install_script("[Unit]\nDescription=x\n", "gaca-api")
    assert "cat > /etc/systemd/system/gaca-api.service <<'GACA_UNIT'\n[Unit]\nDescription=x\nGACA_UNIT" in script
    assert script.index("daemon-reload") < script.index("enable gaca-api") < script.index("restart gaca-api")
    assert "set -euo pipefail" in script


# ── privilege ────────────────────────────────────────────────────────

def test_root_runs_the_script_directly(tmp_path):
    shell = FakeShell(euid=0)
    run_as_root(shell, "echo hi\n", tmp_path / "s.sh")
    assert shell.called("bash", str(tmp_path / "s.sh")) and not shell.called("sudo")


def test_passwordless_sudo_is_used_without_prompting(tmp_path):
    shell = FakeShell(sudo_n=True)
    run_as_root(shell, "echo hi\n", tmp_path / "s.sh")
    assert shell.called("sudo", "-n", "bash", str(tmp_path / "s.sh"))


def test_a_terminal_lets_sudo_ask_for_the_password(tmp_path):
    shell = FakeShell(tty=True)
    run_as_root(shell, "echo hi\n", tmp_path / "s.sh")
    call = [(c, inherit) for c, inherit in shell.calls if c[:2] == ["sudo", "bash"]]
    assert call and call[0][1] is True  # stdio handed over so the prompt is visible


def test_without_any_way_to_get_root_the_script_is_left_for_the_operator(tmp_path):
    shell = FakeShell()
    path = tmp_path / "cfg" / "install-service.sh"
    with pytest.raises(NeedsRoot) as info:
        run_as_root(shell, "echo hi\n", path)
    assert info.value.script == path and path.read_text() == "echo hi\n" and stat.S_IMODE(path.stat().st_mode) == 0o700
    assert not shell.called("sudo", "bash")  # nothing was attempted behind the operator's back


def test_a_failing_root_script_is_an_error(tmp_path):
    with pytest.raises(RuntimeError, match="privileged step failed"):
        run_as_root(FakeShell(euid=0, fail=("s.sh",)), "exit 1\n", tmp_path / "s.sh")


def test_service_mode_choice():
    opts = Options(repo_root=REPO)
    assert choose_service(Options(repo_root=REPO, service="user"), FakeShell()) == "user"
    assert choose_service(opts, FakeShell(sudo_n=True)) == "system"
    assert choose_service(opts, FakeShell(tty=True)) == "system"
    with pytest.raises(RuntimeError) as info:  # no root, no terminal: ask the operator to choose, never downgrade silently
        choose_service(opts, FakeShell())
    assert "--service system" in str(info.value) and "--service user" in str(info.value)


# ── tunnel ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [("gaca.example.com", "gaca.example.com"), (" HTTPS://Gaca.Example.com/ ", "gaca.example.com"),
                                          ("a.b.c.example.org", "a.b.c.example.org")])
def test_hostnames_are_normalised(raw, expected):
    assert tunnel_mod.normalize_hostname(raw) == expected


@pytest.mark.parametrize("bad", ["", "localhost", "gaca", "gaca.example.com/path", "gaca example.com", "a;b.com", "-x.example.com", "https://", "x.example.com:8080"])
def test_bad_hostnames_are_refused(bad):
    with pytest.raises(ValueError):
        tunnel_mod.normalize_hostname(bad)


def test_token_validation():
    good = "eyJhIjoiMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAiLCJ0IjoiYWJj"  # 64 chars of base64
    assert tunnel_mod.valid_token(f"  {good}\n") == good
    for bad in ("", "short", "has space " * 10, "x;rm -rf /" * 8):
        with pytest.raises(ValueError):
            tunnel_mod.valid_token(bad)


def test_token_setup_keeps_the_secret_out_of_the_compose_file(tmp_path):
    token = "eyJhIjoiMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAiLCJ0IjoiYWJj"
    compose = tunnel_mod.write_token_setup(tmp_path / "tunnel", token)
    text = compose.read_text()
    assert token not in text and "--token-file" in text and "network_mode: host" in text and "no-new-privileges" in text
    assert "@sha256:" in text  # pinned, not "latest"
    assert (tmp_path / "tunnel" / "cloudflared.token").read_text().strip() == token
    assert stat.S_IMODE((tmp_path / "tunnel" / "cloudflared.token").stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "tunnel").stat().st_mode) == 0o700


def test_the_quick_tunnel_address_is_found_in_the_logs_after_a_wait():
    shell = FakeShell(logs="INF Requesting new quick Tunnel\nINF |  https://able-quiet-words-123.trycloudflare.com  |")
    assert tunnel_mod.start_quick_tunnel(shell, 41700) == "https://able-quiet-words-123.trycloudflare.com"
    run_cmd = shell.called("docker", "run")[0]
    assert "--url" in run_cmd and "http://127.0.0.1:41700" in run_cmd and "--network" in run_cmd


def test_a_quick_tunnel_with_no_address_fails_clearly():
    with pytest.raises(tunnel_mod.TunnelError, match="no address appeared"):
        tunnel_mod.start_quick_tunnel(FakeShell(logs="nothing useful"), 41700, wait=4)


def test_waiting_for_the_public_url():
    assert tunnel_mod.wait_public(FakeShell(public_status=200), "gaca.example.com") == (True, 200)
    ok, status = tunnel_mod.wait_public(FakeShell(public_status=502), "gaca.example.com", wait=6)
    assert (ok, status) == (False, 502)


def test_without_docker_the_tunnel_step_explains_and_carries_on(tmp_path, capsys):
    opts = Options(repo_root=REPO, tunnel="quick", port=41700, config_dir=tmp_path)
    url = run_mod.setup_tunnel(opts, FakeShell(docker=False), Say(print))
    assert url is None and "Docker is not available" in capsys.readouterr().out


def test_token_tunnel_end_to_end(tmp_path):
    token_file = tmp_path / "tok"
    token_file.write_text("eyJhIjoiMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAiLCJ0IjoiYWJj\n")
    opts = Options(repo_root=REPO, tunnel="token", hostname="Gaca.Example.com", tunnel_token_file=token_file, config_dir=tmp_path / "cfg", port=41700)
    shell = FakeShell()
    url = run_mod.setup_tunnel(opts, shell, Say(lambda *_: None))
    assert url == "https://gaca.example.com"
    assert shell.called("docker", "compose", "-f", str(tmp_path / "cfg" / "tunnel" / "compose.yml"), "up", "-d")


def test_a_token_tunnel_without_a_hostname_is_a_clear_error(tmp_path, monkeypatch):
    token_file = tmp_path / "tok"
    token_file.write_text("eyJhIjoiMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAiLCJ0IjoiYWJj")
    monkeypatch.setattr("sys.stdin", open(os.devnull))
    opts = Options(repo_root=REPO, tunnel="token", tunnel_token_file=token_file, config_dir=tmp_path / "cfg")
    with pytest.raises(RuntimeError, match="--hostname"):
        run_mod.setup_tunnel(opts, FakeShell(), Say(lambda *_: None))


# ── setup info ───────────────────────────────────────────────────────

def test_the_setup_info_shows_the_address_code_link_and_a_qr(capsys):
    print_setup_info(Say(print), "https://gaca.example.com", "ABCD-EFGH-JKMN-PQRS", 41700)
    out = capsys.readouterr().out
    assert "https://gaca.example.com" in out and "ABCD-EFGH-JKMN-PQRS" in out and "█" in out
    assert "Set up this server" in out


# ── the whole flow ───────────────────────────────────────────────────

@pytest.fixture
def flow(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "deploy").mkdir(parents=True)
    for name in ("gaca-api.service", "gaca-api.user.service"):
        (repo / "deploy" / name).write_text((REPO / "deploy" / name).read_text())
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "data" / "gaca.db"))
    return Options(repo_root=repo, data_dir=tmp_path / "data", hermes_home=tmp_path / "home" / ".hermes",
                   config_dir=tmp_path / "home" / ".config" / "gaca", tunnel="none", wait_for_claim=False, yes=True)


def test_a_fresh_install_issues_a_setup_code_and_claims_nothing(flow, capsys):
    shell = FakeShell(sudo_n=True)
    assert run_mod.run(flow, shell) == 0
    out = capsys.readouterr().out
    assert "Setup code:" in out and "█" in out and "Settings" in out and "Service" in out
    assert (flow.repo_root / ".env").exists() and (flow.data_dir.stat().st_mode & 0o777) == 0o700
    assert (flow.config_dir / "install-service.sh").exists() and shell.called("sudo", "-n", "bash")


def test_without_root_the_install_stops_with_the_one_command_to_run(flow, capsys):
    shell = FakeShell(tty=False)
    flow.service = "system"
    assert run_mod.run(flow, shell) == run_mod.EXIT_NEEDS_ROOT
    out = capsys.readouterr().out
    assert f"sudo bash {flow.config_dir / 'install-service.sh'}" in out and "run ./install.sh again" in out
    assert (flow.repo_root / ".env").exists()  # the safe steps already happened, so the re-run resumes


def test_user_mode_needs_no_root(flow, capsys):
    shell = FakeShell()
    flow.service = "user"
    assert run_mod.run(flow, shell) == 0
    assert (Path.home() / ".config" / "systemd" / "user" / "gaca-api.service").exists()
    assert shell.called("systemctl", "--user", "restart", "gaca-api.service") and not shell.called("sudo")
    assert "no filesystem sandbox" in capsys.readouterr().out


def test_a_server_that_does_not_come_up_is_reported_not_ignored(flow, capsys):
    flow.service = "user"
    assert run_mod.run(flow, FakeShell(health=False)) == run_mod.EXIT_FAILED
    assert "did not come up" in capsys.readouterr().out


def test_an_already_claimed_server_is_not_given_a_new_code(flow, capsys):
    flow.service = "user"
    assert run_mod.run(flow, FakeShell(claimed=lambda: True)) == 0
    out = capsys.readouterr().out
    assert "already has an owner" in out and "Setup code" not in out


def test_the_installer_waits_for_the_claim_then_wires_hermes(flow, capsys):
    (flow.hermes_home / "profiles" / "toolshed").mkdir(parents=True)
    (flow.hermes_home / ".env").write_text("OPENAI_API_KEY=keep\n")
    flow.service, flow.wait_for_claim = "user", True
    state = {"claimed": False}
    shell = FakeShell(claimed=lambda: state["claimed"])

    def someone_claims_in_the_app():
        async def create():
            await client.connect()
            try:
                await auth_service.create_user(name="O", email="o@example.com", password="correct horse")
            finally:
                await client.close()
        if not state["claimed"]:
            asyncio.run(create())
            state["claimed"] = True
    shell.on_sleep = someone_claims_in_the_app
    assert run_mod.run(flow, shell) == 0
    out = capsys.readouterr().out
    assert "account created" in out and "scoped GACA token" in out
    for env in (flow.hermes_home / ".env", flow.hermes_home / "profiles" / "toolshed" / ".env"):
        assert "GACA_API_TOKEN=gaca_pat_" in env.read_text()
    assert "OPENAI_API_KEY=keep" in (flow.hermes_home / ".env").read_text()


def test_no_hermes_is_not_an_error(flow, capsys):
    flow.service, flow.wait_for_claim = "user", False
    assert run_mod.run(flow, FakeShell()) == 0
    assert "gaca-api hermes connect" in capsys.readouterr().out
