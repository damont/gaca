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

from conftest import make_owner
from gaca_api.config import settings
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
                 logs="", public_status=200, fail=(), hermes=None, gateway=True, started_at=None):
        # `hermes`: the version string `hermes --version` prints, or None for "not installed".
        # `hermes_after_install`: what appears once the official installer has run.
        self._euid, self._tty, self._sudo_n, self._docker = euid, tty, sudo_n, docker
        self.hermes, self.gateway, self.hermes_after_install = hermes, gateway, None
        self.started_at = started_at  # when the system service last started (None: not running)
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
        elif argv[1:2] == ["--version"] and argv[0].endswith("hermes"):
            out = f"Hermes Agent {self.hermes}\n" if self.hermes else ""
            rc = 0 if self.hermes else 1
        elif argv[:2] == ["bash", "-c"] and "hermes-agent.nousresearch.com" in argv[2]:
            self.hermes = self.hermes_after_install or self.hermes
        elif any(f in " ".join(argv) for f in self.fail):
            rc = 1
        return subprocess.CompletedProcess(argv, rc, out, "boom" if rc else "")

    def which(self, name):
        if name == "hermes":
            return "/usr/bin/hermes" if self.hermes else None
        return f"/usr/bin/{name}" if (name != "docker" or self._docker) else None

    def is_tty(self): return self._tty
    def euid(self): return self._euid
    def service_started_at(self, name): return self.started_at

    def sleep(self, seconds):
        self.sleeps += 1
        self.on_sleep()

    def http_get(self, url, timeout=8):
        if url.endswith("/api/health") and url.startswith("http://127.0.0.1"):
            return (200, '{"service":"gaca-api"}') if self.health else (0, "")
        if url.endswith("/api/setup"):
            return 200, json.dumps({"claimed": bool(self.claimed())})
        if url.endswith("/health") and ":8642" in url:
            return (200, "ok") if self.gateway else (0, "")
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


def test_a_fresh_env_gets_everything_the_server_needs(tmp_path):
    path = tmp_path / ".env"
    notes = ensure_env(path, **env_kwargs(tmp_path, hermes_bin=None))
    values = read_env(path)
    assert "GACA_JWT_SECRET" not in values  # there are no sessions to sign any more
    assert values["GACA_DB_PATH"] == str(tmp_path / "data" / "gaca.db")
    assert values["GACA_AGENT_ACCESS"] == "local" and values["GACA_AGENT_ENGINE"] == "hermes"
    assert values["GACA_API_HOST"] == "127.0.0.1" and "GACA_HERMES_BIN" not in values
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and any("wrote" in n for n in notes)


def test_rerunning_keeps_everything_as_it_was(tmp_path):
    path = tmp_path / ".env"
    ensure_env(path, **env_kwargs(tmp_path))
    path.write_text(path.read_text() + "MY_OWN_SETTING=keep-me\nGACA_API_PORT=9999\n")
    before = read_env(path)
    notes = ensure_env(path, **env_kwargs(tmp_path))
    after = read_env(path)
    assert after == before  # nothing changed...
    assert after["GACA_API_PORT"] == "9999" and after["MY_OWN_SETTING"] == "keep-me"  # ...including the operator's own edits
    assert notes == [".env already complete"]


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


GOOD_TOKEN = "eyJhIjoiMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAiLCJ0IjoiYWJj"


@pytest.mark.parametrize("pasted", [
    f"cloudflared tunnel run --token {GOOD_TOKEN}",
    f"  cloudflared tunnel run --token {GOOD_TOKEN}\n",
    f"sudo cloudflared service install {GOOD_TOKEN}",
    f"sudo cloudflared service install {GOOD_TOKEN}\n\ncloudflared tunnel run --token {GOOD_TOKEN}\n",  # both boxes at once
    f"cloudflared tunnel run --token={GOOD_TOKEN}",
])
def test_the_command_cloudflare_shows_works_as_well_as_the_bare_token(pasted):
    assert tunnel_mod.valid_token(pasted) == GOOD_TOKEN


@pytest.mark.parametrize("pasted", [
    f"cloudflared tunnel run --token {GOOD_TOKEN}; rm -rf /",  # the token is used, the rest is never run or kept
])
def test_only_the_token_is_taken_from_a_pasted_command(pasted):
    assert tunnel_mod.valid_token(pasted) == GOOD_TOKEN


def test_the_masked_display_text_is_explained_not_accepted():
    with pytest.raises(ValueError, match="copy button"):
        tunnel_mod.valid_token("cloudflared tunnel run --token eyJhIjoi*********************bCJ9")


def test_two_different_tokens_in_one_paste_are_refused():
    other = GOOD_TOKEN[:-3] + "xyz"
    with pytest.raises(ValueError):
        tunnel_mod.valid_token(f"cloudflared tunnel run --token {GOOD_TOKEN}\ncloudflared tunnel run --token {other}")


def test_the_token_file_may_hold_the_whole_command(tmp_path):
    token_file = tmp_path / "tok"
    token_file.write_text(f"cloudflared tunnel run --token {GOOD_TOKEN}\n")
    opts = Options(repo_root=REPO, tunnel="token", hostname="gaca.example.com", tunnel_token_file=token_file, config_dir=tmp_path / "cfg", port=41700)
    shell = FakeShell()
    assert run_mod.setup_tunnel(opts, shell, Say(lambda *_: None)) == "https://gaca.example.com"
    stored = (tmp_path / "cfg" / "tunnel" / "cloudflared.token").read_text().strip()
    assert stored == GOOD_TOKEN  # only the token was stored, not the command around it


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

def test_the_pairing_info_shows_the_address_code_and_a_qr(capsys):
    print_setup_info(Say(print), "https://gaca.example.com", "ABCD-EFGH-JKMN-PQRS", 41700)
    out = capsys.readouterr().out
    assert "https://gaca.example.com" in out and "ABCD-EFGH-JKMN-PQRS" in out and "█" in out
    assert "Pairing code:" in out and "gaca-api pair" in out


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
                   config_dir=tmp_path / "home" / ".config" / "gaca", tunnel="none", wait_for_claim=False, yes=True,
                   use_hermes=False)  # the Hermes requirements have their own tests, below


def test_a_fresh_install_issues_a_pairing_code_and_pairs_nothing(flow, capsys):
    shell = FakeShell(sudo_n=True)
    assert run_mod.run(flow, shell) == 0
    out = capsys.readouterr().out
    assert "Pairing code:" in out and "█" in out and "Settings" in out and "Service" in out
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


def test_a_server_with_a_paired_phone_is_not_given_a_new_code(flow, capsys):
    flow.service = "user"
    assert run_mod.run(flow, FakeShell(claimed=lambda: True)) == 0
    out = capsys.readouterr().out
    assert "already paired" in out and "Pairing code" not in out and "gaca-api pair" in out


def test_the_installer_waits_for_the_first_pairing_then_wires_hermes(flow, capsys):
    (flow.hermes_home / "profiles" / "toolshed").mkdir(parents=True)
    (flow.hermes_home / ".env").write_text("OPENAI_API_KEY=keep\nAPI_SERVER_ENABLED=true\nAPI_SERVER_KEY=" + "k" * 32 + "\n")
    flow.service, flow.wait_for_claim, flow.use_hermes = "user", True, True
    state = {"claimed": False}
    shell = FakeShell(claimed=lambda: state["claimed"], hermes="v0.21.5")

    def someone_pairs_in_the_app():
        async def create():
            await client.connect()
            try:
                await make_owner(name="O", email="o@example.com")
            finally:
                await client.close()
        if not state["claimed"]:
            asyncio.run(create())
            state["claimed"] = True
    shell.on_sleep = someone_pairs_in_the_app
    assert run_mod.run(flow, shell) == 0
    out = capsys.readouterr().out
    assert "phone paired" in out and "scoped GACA token" in out
    assert "crew in the app: default (default)" in out  # the default profile is registered, nobody ran a discovery step
    for env in (flow.hermes_home / ".env", flow.hermes_home / "profiles" / "toolshed" / ".env"):
        assert "GACA_API_TOKEN=gaca_pat_" in env.read_text()
    assert "OPENAI_API_KEY=keep" in (flow.hermes_home / ".env").read_text()


def test_no_hermes_is_not_an_error(flow, capsys):
    flow.service, flow.wait_for_claim = "user", False
    assert run_mod.run(flow, FakeShell()) == 0
    assert "gaca-api hermes connect" in capsys.readouterr().out


# ── uninstall ────────────────────────────────────────────────────────

@pytest.fixture
def installed(flow, monkeypatch, tmp_path):
    """A user-mode install laid out on disk (via the real installer), plus a fake system unit dir."""
    flow.service = "user"
    assert run_mod.run(flow, FakeShell()) == 0
    monkeypatch.setattr(run_mod, "SYSTEM_UNIT_DIR", tmp_path / "etc-systemd")
    (flow.config_dir / "tunnel").mkdir(parents=True, exist_ok=True)
    (flow.config_dir / "tunnel" / "compose.yml").write_text("name: x\n")
    (flow.config_dir / "tunnel" / "cloudflared.token").write_text("secret-token\n")
    return flow


def test_uninstall_removes_the_service_and_tunnel_but_keeps_data(installed, capsys):
    shell = FakeShell()
    unit = Path.home() / ".config" / "systemd" / "user" / "gaca-api.service"
    assert unit.exists()
    assert run_mod.uninstall(installed, shell) == 0
    out = capsys.readouterr().out
    assert not unit.exists()
    assert "no service found" not in out  # it found one and removed it: don't claim otherwise
    assert shell.called("systemctl", "--user", "disable", "--now", "gaca-api.service")
    assert shell.called("docker", "rm", "-f", "gaca-quick-tunnel") and shell.called("docker", "compose")
    assert not (installed.config_dir / "tunnel").exists()  # the stored tunnel token is gone
    assert installed.data_dir.exists() and (installed.repo_root / ".env").exists()
    assert "untouched" in out and "--purge" in out


def test_purge_deletes_the_database_and_secrets(installed):
    (installed.data_dir / "gaca.db").write_text("chats")
    assert run_mod.uninstall(installed, FakeShell(), purge=True) == 0
    assert not installed.data_dir.exists() and not (installed.repo_root / ".env").exists()


def test_uninstall_twice_is_fine(installed, capsys):
    run_mod.uninstall(installed, FakeShell())
    capsys.readouterr()
    assert run_mod.uninstall(installed, FakeShell()) == 0
    assert "no service found" in capsys.readouterr().out


def test_removing_a_system_service_without_root_leaves_one_command(installed, tmp_path, capsys):
    (tmp_path / "etc-systemd").mkdir()
    (tmp_path / "etc-systemd" / "gaca-api.service").write_text("[Unit]\n")
    shell = FakeShell()
    assert run_mod.uninstall(installed, shell) == run_mod.EXIT_NEEDS_ROOT
    script = installed.config_dir / "uninstall-service.sh"
    assert "sudo bash" in capsys.readouterr().out and "systemctl disable --now gaca-api" in script.read_text()
    assert not shell.called("sudo", "bash")


def test_purge_from_the_command_line_needs_confirmation(installed, monkeypatch, capsys):
    from gaca_api import cli
    monkeypatch.setattr("sys.stdin", open(os.devnull))  # no terminal, no --yes
    code = cli.main(["uninstall", "--repo-root", str(installed.repo_root), "--data-dir", str(installed.data_dir), "--purge"])
    assert code == 1 and installed.data_dir.exists()
    assert "Not purging" in capsys.readouterr().err


def test_the_public_address_is_saved_so_pair_can_put_it_in_the_qr_code(flow, monkeypatch):
    flow.service = "user"
    monkeypatch.setattr(run_mod, "setup_tunnel", lambda opts, shell, say: "https://gaca.example.com")
    assert run_mod.run(flow, FakeShell()) == 0
    assert read_env(flow.repo_root / ".env")["GACA_PUBLIC_URL"] == "https://gaca.example.com"


# ── Hermes is required, and GACA says what is missing ────────────────

def ready_hermes(flow, **env_over):
    flow.hermes_home.mkdir(parents=True, exist_ok=True)
    env = {"API_SERVER_ENABLED": "true", "API_SERVER_KEY": "k" * 32, **env_over}
    (flow.hermes_home / ".env").write_text("".join(f"{k}={v}\n" for k, v in env.items() if v is not None))
    flow.use_hermes = True
    flow.service = "user"


def installed_nothing(flow):
    return not (flow.repo_root / ".env").exists() and not (flow.config_dir / "install-service.sh").exists()


def test_without_hermes_the_install_stops_first_and_says_how_to_get_it(flow, capsys):
    flow.use_hermes, flow.service = True, "user"
    shell = FakeShell()
    assert run_mod.run(flow, shell) == run_mod.EXIT_NEEDS_HERMES
    out = capsys.readouterr().out
    assert "Hermes is not installed" in out and "curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash" in out
    assert "hermes setup" in out and "run ./install.sh again" in out
    assert installed_nothing(flow) and not shell.called("systemctl")  # nothing was set up before the requirement was met


def test_an_old_hermes_is_refused_with_the_version_it_found(flow, capsys):
    ready_hermes(flow)
    assert run_mod.run(flow, FakeShell(hermes="v0.20.3")) == run_mod.EXIT_NEEDS_HERMES
    out = capsys.readouterr().out
    assert "0.21 or newer (found 0.20.3)" in out and installed_nothing(flow)


def test_a_hermes_that_was_never_set_up_is_told_to_run_hermes_setup(flow, capsys):
    flow.use_hermes, flow.service = True, "user"
    assert not flow.hermes_home.exists()
    assert run_mod.run(flow, FakeShell(hermes="v0.21.5")) == run_mod.EXIT_NEEDS_HERMES
    out = capsys.readouterr().out
    assert "has not been set up yet" in out and "hermes setup" in out and installed_nothing(flow)


def test_a_disabled_api_server_gets_the_exact_lines_to_add_and_never_echoes_a_key(flow, capsys):
    ready_hermes(flow, API_SERVER_ENABLED=None, API_SERVER_KEY=None)
    (flow.hermes_home / ".env").write_text("OPENAI_API_KEY=sk-super-secret\n")
    assert run_mod.run(flow, FakeShell(hermes="v0.21.5")) == run_mod.EXIT_NEEDS_HERMES
    out = capsys.readouterr().out
    assert "API server is not switched on" in out and "API_SERVER_ENABLED=true" in out and "API_SERVER_KEY=<your long random string>" in out
    assert "hermes gateway install" in out and "enable-linger" in out and "sk-super-secret" not in out and installed_nothing(flow)


def test_an_enabled_api_server_without_a_key_is_not_enough(flow, capsys):
    ready_hermes(flow, API_SERVER_KEY="")
    assert run_mod.run(flow, FakeShell(hermes="v0.21.5")) == run_mod.EXIT_NEEDS_HERMES
    assert "API server is not switched on" in capsys.readouterr().out


def test_a_gateway_that_is_not_running_is_reported(flow, capsys):
    ready_hermes(flow)
    assert run_mod.run(flow, FakeShell(hermes="v0.21.5", gateway=False)) == run_mod.EXIT_NEEDS_HERMES
    out = capsys.readouterr().out
    assert "not answering on 127.0.0.1:8642" in out and "systemctl --user start hermes-gateway" in out and "enable-linger" in out


def test_a_custom_gateway_port_is_the_one_that_is_checked(flow, capsys):
    ready_hermes(flow, API_SERVER_PORT="9000")
    shell = FakeShell(hermes="v0.21.5")
    original = shell.http_get
    shell.http_get = lambda url, timeout=8: (200, "ok") if url == "http://127.0.0.1:9000/health" else original(url, timeout)
    assert run_mod.run(flow, shell) == 0
    assert "answers on 127.0.0.1:9000" in capsys.readouterr().out


def test_a_ready_hermes_passes_and_gets_the_skills(flow, capsys):
    (flow.hermes_home / "profiles" / "research").mkdir(parents=True)
    ready_hermes(flow)
    assert run_mod.run(flow, FakeShell(hermes="v0.21.5")) == 0
    out = capsys.readouterr().out
    assert "Hermes 0.21.5 is installed and its gateway answers" in out
    for place in (flow.hermes_home / "skills" / "gaca", flow.hermes_home / "profiles" / "research" / "skills" / "gaca"):
        assert (place / "gaca-alerts" / "SKILL.md").is_file() and (place / "gaca-briefing" / "scripts" / "briefing.py").is_file()
    assert "installed the GACA skills into Hermes profile 'default'" in out and "'research'" in out


def test_a_second_run_leaves_skills_alone_and_says_so(flow, capsys):
    ready_hermes(flow)
    run_mod.run(flow, FakeShell(hermes="v0.21.5"))
    capsys.readouterr()
    mine = flow.hermes_home / "skills" / "gaca" / "gaca-alerts" / "SKILL.md"
    mine.write_text("agent edit")
    run_mod.run(flow, FakeShell(hermes="v0.21.5"))
    assert mine.read_text() == "agent edit"
    assert "edited were left alone" in capsys.readouterr().out


def test_install_hermes_runs_only_its_official_installer_then_still_asks_for_setup(flow, capsys):
    flow.use_hermes, flow.service, flow.install_hermes = True, "user", True
    shell = FakeShell()
    shell.hermes_after_install = "v0.21.5"
    assert run_mod.run(flow, shell) == run_mod.EXIT_NEEDS_HERMES  # installed, but never set up
    out = capsys.readouterr().out
    assert [c for c in shell.called("bash", "-c")][0][2] == "curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash"
    assert "hermes setup" in out and installed_nothing(flow)


def test_install_hermes_carries_on_when_the_install_leaves_everything_ready(flow, capsys):
    ready_hermes(flow)
    flow.install_hermes = True
    shell = FakeShell()  # not installed yet; the installer makes it so
    shell.hermes_after_install = "v0.21.5"
    assert run_mod.run(flow, shell) == 0
    assert shell.called("bash", "-c") and "Hermes 0.21.5 is installed" in capsys.readouterr().out


def test_without_the_flag_nothing_third_party_runs(flow, capsys):
    flow.use_hermes, flow.service = True, "user"
    shell = FakeShell()
    assert run_mod.run(flow, shell) == run_mod.EXIT_NEEDS_HERMES
    assert not shell.called("bash", "-c")


def test_at_a_terminal_it_asks_before_running_the_hermes_installer(flow, capsys, monkeypatch):
    flow.use_hermes, flow.service, flow.yes = True, "user", False
    answers = iter(["n"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    shell = FakeShell(tty=True)
    assert run_mod.run(flow, shell) == run_mod.EXIT_NEEDS_HERMES
    assert not shell.called("bash", "-c")  # said no
    answers = iter(["y"])
    shell = FakeShell(tty=True)
    shell.hermes_after_install = "v0.21.5"
    run_mod.run(flow, shell)
    assert shell.called("bash", "-c")  # said yes


def test_no_hermes_skips_the_checks_but_says_what_that_costs(flow, capsys):
    flow.use_hermes, flow.service = False, "user"
    assert run_mod.run(flow, FakeShell()) == 0
    out = capsys.readouterr().out
    assert "skipping the Hermes checks" in out and "no agent to talk to" in out


# ── push notifications (optional, asked about at setup) ──────────────

import json as _json

from gaca_api.installer import push as push_mod

PEM = "-----BEGIN PRIVATE KEY-----\nMIGTAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBHkwdwIBAQQg\n-----END PRIVATE KEY-----\n"


def fcm_key(tmp_path, **over):
    data = {"type": "service_account", "project_id": "my-gaca", "client_email": "svc@my-gaca.iam.gserviceaccount.com",
            "private_key": PEM, **over}
    path = tmp_path / "firebase.json"
    path.write_text(_json.dumps({k: v for k, v in data.items() if v is not None}))
    return path


def p8_key(tmp_path, text=PEM):
    path = tmp_path / "AuthKey_ABC123DEFG.p8"
    path.write_text(text)
    return path


def push_env(flow):
    return read_env(flow.repo_root / ".env")


def ask_with(monkeypatch, *answers):
    """Feed `input()` these answers; any question beyond them fails the test."""
    supply = iter(answers)
    asked = []

    def fake(prompt=""):
        asked.append(prompt)
        return next(supply)
    monkeypatch.setattr("builtins.input", fake)
    return asked


def test_android_push_from_a_flag_installs_the_key_privately_and_points_env_at_it(flow, tmp_path, capsys):
    flow.service, flow.fcm_credentials = "user", fcm_key(tmp_path)
    assert run_mod.run(flow, FakeShell()) == 0
    secret = flow.config_dir / "secrets" / "fcm-service-account.json"
    assert push_env(flow)["GACA_FCM_CREDENTIALS_PATH"] == str(secret)
    assert stat.S_IMODE(secret.stat().st_mode) == 0o600 and stat.S_IMODE(secret.parent.stat().st_mode) == 0o700
    out = capsys.readouterr().out
    assert "Android push is set up (Firebase project my-gaca)" in out
    assert "MIGTAgEAMBMG" not in out  # the key is never printed


def test_iphone_push_from_flags_writes_all_four_settings(flow, tmp_path, capsys):
    flow.service = "user"
    flow.apns_key, flow.apns_key_id, flow.apns_team_id, flow.apns_topic = p8_key(tmp_path), "abc123defg", "TEAM123456", "com.example.gaca"
    assert run_mod.run(flow, FakeShell()) == 0
    env = push_env(flow)
    secret = flow.config_dir / "secrets" / "apns-key.p8"
    assert env["GACA_APNS_KEY_PATH"] == str(secret) and env["GACA_APNS_KEY_ID"] == "ABC123DEFG"
    assert env["GACA_APNS_TEAM_ID"] == "TEAM123456" and env["GACA_APNS_TOPIC"] == "com.example.gaca"
    assert stat.S_IMODE(secret.stat().st_mode) == 0o600
    out = capsys.readouterr().out
    assert "iPhone push is set up" in out and "MIGTAgEAMBMG" not in out


def test_both_platforms_can_be_set_up_together(flow, tmp_path):
    flow.service, flow.fcm_credentials = "user", fcm_key(tmp_path)
    flow.apns_key, flow.apns_key_id, flow.apns_team_id, flow.apns_topic = p8_key(tmp_path), "ABC123DEFG", "TEAM123456", "com.example.gaca"
    assert run_mod.run(flow, FakeShell()) == 0
    env = push_env(flow)
    assert "GACA_FCM_CREDENTIALS_PATH" in env and "GACA_APNS_KEY_PATH" in env


def test_a_bad_android_key_fails_the_install_with_a_reason_and_installs_nothing(flow, tmp_path, capsys):
    flow.service, flow.fcm_credentials = "user", fcm_key(tmp_path, type="authorized_user")
    assert run_mod.run(flow, FakeShell()) == run_mod.EXIT_FAILED
    out = capsys.readouterr().out
    assert "push notification setup failed" in out and "service_account" in out
    assert not (flow.config_dir / "secrets").exists() and "GACA_FCM_CREDENTIALS_PATH" not in push_env(flow)


def test_partial_iphone_flags_are_refused(flow, tmp_path, capsys):
    flow.service, flow.apns_key, flow.apns_key_id = "user", p8_key(tmp_path), "ABC123DEFG"
    assert run_mod.run(flow, FakeShell()) == run_mod.EXIT_FAILED
    assert "together" in capsys.readouterr().out and not (flow.config_dir / "secrets").exists()


def test_at_a_terminal_it_asks_per_platform_and_says_whose_account_it_must_be(flow, tmp_path, monkeypatch, capsys):
    flow.service, flow.yes = "user", False
    asked = ask_with(monkeypatch, "y", str(fcm_key(tmp_path)), "y", str(p8_key(tmp_path)), "abc123defg", "TEAM123456", "com.example.gaca")
    assert run_mod.run(flow, FakeShell(tty=True)) == 0
    env, out = push_env(flow), capsys.readouterr().out
    assert env["GACA_FCM_CREDENTIALS_PATH"].endswith("fcm-service-account.json") and env["GACA_APNS_TOPIC"] == "com.example.gaca"
    assert "SAME developer account" in out and "same Firebase project" in out
    assert asked[0].startswith("Set up push notifications for Android?") and asked[2].startswith("Set up push notifications for iPhone?")


def test_the_default_answer_is_no_and_nothing_is_set_up(flow, monkeypatch, capsys):
    flow.service, flow.yes = "user", False
    ask_with(monkeypatch, "", "n")
    assert run_mod.run(flow, FakeShell(tty=True)) == 0
    env = push_env(flow)
    assert not any(k.startswith(("GACA_FCM", "GACA_APNS")) for k in env)
    assert "push notifications are off" in capsys.readouterr().out


def test_a_wrong_path_is_reasked_and_a_blank_gives_up(flow, tmp_path, monkeypatch, capsys):
    flow.service, flow.yes = "user", False
    ask_with(monkeypatch, "y", str(tmp_path / "nope.json"), str(fcm_key(tmp_path)), "n")
    assert run_mod.run(flow, FakeShell(tty=True)) == 0
    out = capsys.readouterr().out
    assert "cannot read" in out and "Android push is set up" in out
    flow.config_dir.joinpath("secrets", "fcm-service-account.json").unlink()
    (flow.repo_root / ".env").write_text((flow.repo_root / ".env").read_text().replace("GACA_FCM_CREDENTIALS_PATH=", "X_OLD="))
    ask_with(monkeypatch, "y", "", "n")
    assert run_mod.run(flow, FakeShell(tty=True)) == 0
    assert "Android push skipped" in capsys.readouterr().out


def test_three_wrong_iphone_answers_skip_it_without_failing_the_install(flow, tmp_path, monkeypatch, capsys):
    flow.service, flow.yes = "user", False
    ask_with(monkeypatch, "n", "y", str(p8_key(tmp_path)), "bad", "worse", "nope")
    assert run_mod.run(flow, FakeShell(tty=True)) == 0
    out = capsys.readouterr().out
    assert "10 letters and digits" in out and "iPhone push skipped" in out and "GACA_APNS_KEY_PATH" not in push_env(flow)


def test_running_again_does_not_re_ask_what_is_already_set_up(flow, tmp_path, monkeypatch, capsys):
    flow.service, flow.fcm_credentials = "user", fcm_key(tmp_path)
    run_mod.run(flow, FakeShell())
    capsys.readouterr()
    flow.fcm_credentials, flow.yes = None, False
    asked = ask_with(monkeypatch, "n")  # only the iPhone question may come up
    assert run_mod.run(flow, FakeShell(tty=True)) == 0
    assert len(asked) == 1 and asked[0].startswith("Set up push notifications for iPhone?")
    assert "Android push is already set up" in capsys.readouterr().out


def test_no_push_flag_and_no_terminal_never_ask(flow, monkeypatch, capsys):
    flow.service = "user"
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("must not ask"))
    flow.no_push, flow.yes = True, False
    assert run_mod.run(flow, FakeShell(tty=True)) == 0 and "skipped (--no-push)" in capsys.readouterr().out
    flow.no_push = False
    assert run_mod.run(flow, FakeShell(tty=False)) == 0 and "push notifications are off" in capsys.readouterr().out


def test_purge_removes_the_push_keys_too(flow, tmp_path, capsys):
    flow.service, flow.fcm_credentials = "user", fcm_key(tmp_path)
    run_mod.run(flow, FakeShell())
    assert (flow.config_dir / "secrets" / "fcm-service-account.json").exists()
    assert run_mod.uninstall(flow, FakeShell(), purge=True) == 0
    assert not (flow.config_dir / "secrets").exists()


def test_the_key_checks(tmp_path):
    assert push_mod.check_fcm(fcm_key(tmp_path)) == "my-gaca"
    for bad in (dict(type="authorized_user"), dict(project_id=None), dict(private_key="not a key"), dict(client_email="")):
        with pytest.raises(push_mod.PushSetupError):
            push_mod.check_fcm(fcm_key(tmp_path, **bad))
    junk = tmp_path / "junk.json"
    junk.write_text("{nope")
    for path in (junk, tmp_path / "missing.json", tmp_path):
        with pytest.raises(push_mod.PushSetupError):
            push_mod.check_fcm(path)
    big = tmp_path / "big.json"
    big.write_text(" " * 20_000)
    with pytest.raises(push_mod.PushSetupError):
        push_mod.check_fcm(big)
    push_mod.check_apns_key(p8_key(tmp_path))
    with pytest.raises(push_mod.PushSetupError):
        push_mod.check_apns_key(p8_key(tmp_path, "just some text"))
    assert push_mod.check_apple_id(" abc123defg ", "key id") == "ABC123DEFG"
    for bad in ("short", "ABC123DEFGH", "ABC-23DEFG", ""):
        with pytest.raises(push_mod.PushSetupError):
            push_mod.check_apple_id(bad, "key id")
    assert push_mod.check_bundle_id("com.example.gaca") == "com.example.gaca"
    for bad in ("gaca", "com..x", "com.example gaca", "com.exa/mple", ""):
        with pytest.raises(push_mod.PushSetupError):
            push_mod.check_bundle_id(bad)


def test_installing_a_secret_replaces_the_old_one_privately(tmp_path):
    first, second = fcm_key(tmp_path), tmp_path / "second.json"
    second.write_text(first.read_text().replace("my-gaca", "other"))
    target = push_mod.install_secret(first, tmp_path / "s", "k.json")
    push_mod.install_secret(second, tmp_path / "s", "k.json")
    assert "other" in target.read_text() and stat.S_IMODE(target.stat().st_mode) == 0o600
    assert [p.name for p in (tmp_path / "s").iterdir()] == ["k.json"]  # no temp files left behind


def test_a_setting_that_points_at_a_deleted_key_is_asked_about_again(flow, tmp_path, monkeypatch, capsys):
    flow.service, flow.fcm_credentials = "user", fcm_key(tmp_path)
    run_mod.run(flow, FakeShell())
    (flow.config_dir / "secrets" / "fcm-service-account.json").unlink()  # .env still names it
    capsys.readouterr()
    flow.fcm_credentials, flow.yes = None, False
    asked = ask_with(monkeypatch, "n", "n")
    assert run_mod.run(flow, FakeShell(tty=True)) == 0
    assert asked[0].startswith("Set up push notifications for Android?")
    assert "already set up" not in capsys.readouterr().out


# ── `gaca-api` on the PATH ───────────────────────────────────────────

def fake_venv(flow):
    exe = flow.repo_root / "gaca-api" / ".venv" / "bin" / "gaca-api"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    return exe


def test_the_install_puts_gaca_api_on_the_path_and_lists_the_everyday_commands(flow, capsys, monkeypatch):
    flow.service = "user"
    exe = fake_venv(flow)
    monkeypatch.setenv("SHELL", "/bin/bash")
    assert run_mod.run(flow, FakeShell()) == 0
    link = Path.home() / ".local" / "bin" / "gaca-api"
    assert link.is_symlink() and link.resolve() == exe.resolve()
    out = capsys.readouterr().out
    assert "added ~/.local/bin to your PATH" in out and "open a new shell" in out
    for command in ("gaca-api doctor", "gaca-api pair", "gaca-api push", "gaca-api service", "gaca-api backup"):
        assert command in out
    assert run_mod.run(flow, FakeShell()) == 0  # again: already linked, no complaint
    assert "left alone" not in capsys.readouterr().out


def test_a_gaca_api_command_that_is_not_ours_is_never_replaced(flow, capsys):
    flow.service = "user"
    fake_venv(flow)
    mine = Path.home() / ".local" / "bin" / "gaca-api"
    mine.parent.mkdir(parents=True)
    mine.write_text("#!/bin/sh\necho mine\n")
    assert run_mod.run(flow, FakeShell()) == 0
    assert mine.read_text() == "#!/bin/sh\necho mine\n" and "left alone" in capsys.readouterr().out


def test_uninstall_removes_only_its_own_link(flow, capsys):
    flow.service = "user"
    fake_venv(flow)
    run_mod.run(flow, FakeShell())
    link = Path.home() / ".local" / "bin" / "gaca-api"
    assert link.is_symlink()
    run_mod.uninstall(flow, FakeShell())
    assert not link.exists()
    link.parent.mkdir(parents=True, exist_ok=True)
    link.write_text("not ours")
    run_mod.uninstall(flow, FakeShell())
    assert link.read_text() == "not ours"


# ── ~/.local/bin is added to the PATH automatically ──────────────────

def path_lines(rc):
    return [l for l in rc.read_text().splitlines() if ".local/bin" in l and not l.startswith("#")]


@pytest.mark.parametrize("shell, rc_name", [("/bin/bash", ".bashrc"), ("/usr/bin/zsh", ".zshrc"), ("/bin/dash", ".profile")])
def test_the_path_line_goes_in_the_right_startup_file_once(flow, monkeypatch, capsys, shell, rc_name):
    monkeypatch.setenv("SHELL", shell)
    rc = Path.home() / rc_name
    rc.write_text("alias ll='ls -l'\nexport EDITOR=vim")  # no trailing newline, to check it is not glued to ours
    run_mod.ensure_on_path(Say(print))
    text = rc.read_text()
    assert text.startswith("alias ll='ls -l'\nexport EDITOR=vim\n") and len(path_lines(rc)) == 1
    assert 'export PATH="$HOME/.local/bin:$PATH"' in text and run_mod._PATH_MARK in text
    out = capsys.readouterr().out
    assert f"added ~/.local/bin to your PATH in {rc}" in out and f"source {rc}" in out
    run_mod.ensure_on_path(Say(print))  # again: nothing more is written
    assert rc.read_text() == text and "already set up" in capsys.readouterr().out


def test_a_missing_startup_file_is_created(flow, monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/bash")
    run_mod.ensure_on_path(Say(lambda *_: None))
    assert len(path_lines(Path.home() / ".bashrc")) == 1


def test_fish_gets_fish_syntax(flow, monkeypatch):
    monkeypatch.setenv("SHELL", "/usr/bin/fish")
    run_mod.ensure_on_path(Say(lambda *_: None))
    assert path_lines(Path.home() / ".config" / "fish" / "config.fish") == ["fish_add_path -g $HOME/.local/bin"]


def test_a_startup_file_that_already_handles_local_bin_is_left_alone(flow, monkeypatch, capsys):
    monkeypatch.setenv("SHELL", "/bin/bash")
    rc = Path.home() / ".bashrc"
    rc.write_text('. "$HOME/.local/bin/env"\n')  # what the uv installer writes
    before = rc.read_text()
    run_mod.ensure_on_path(Say(print))
    assert rc.read_text() == before and "already set up" in capsys.readouterr().out


def test_a_symlinked_startup_file_is_never_edited(flow, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("SHELL", "/bin/bash")
    dotfiles = tmp_path / "dotfiles-bashrc"
    dotfiles.write_text("# managed elsewhere\n")
    (Path.home() / ".bashrc").symlink_to(dotfiles)
    run_mod.ensure_on_path(Say(print))
    assert dotfiles.read_text() == "# managed elsewhere\n"
    out = capsys.readouterr().out
    assert "is a symlink" in out and 'export PATH="$HOME/.local/bin:$PATH"' in out


def test_the_install_adds_the_path_automatically(flow, monkeypatch):
    flow.service = "user"
    fake_venv(flow)
    monkeypatch.setenv("SHELL", "/bin/bash")
    assert run_mod.run(flow, FakeShell()) == 0
    assert len(path_lines(Path.home() / ".bashrc")) == 1


# ── health checks look like a browser (Cloudflare 403s Python's default User-Agent) ──

def test_http_get_sends_a_browser_like_user_agent(monkeypatch):
    from gaca_api.installer import shell as shell_mod

    seen = {}

    class Response:
        status = 200

        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, n): return b"ok"

    def fake_urlopen(request, timeout=0):
        seen["ua"], seen["url"] = request.get_header("User-agent"), request.full_url
        return Response()

    monkeypatch.setattr(shell_mod.urllib.request, "urlopen", fake_urlopen)
    assert shell_mod.Shell().http_get("https://gaca.example.com/api/health") == (200, "ok")
    assert seen["ua"].startswith("Mozilla/5.0") and "Python-urllib" not in seen["ua"]
    assert seen["url"] == "https://gaca.example.com/api/health"


def test_the_interactive_token_prompt_uses_the_current_cloudflare_wording(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", type("TTY", (), {"isatty": lambda self: True, "fileno": lambda self: 0})())
    monkeypatch.setattr(run_mod.getpass, "getpass", lambda prompt="": "eyJhIjoiMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAiLCJ0IjoiYWJj")
    monkeypatch.setattr("builtins.input", lambda prompt="": "gaca.example.com")
    opts = Options(repo_root=REPO, tunnel="token", config_dir=tmp_path / "cfg", port=41700)
    assert run_mod.setup_tunnel(opts, FakeShell(), Say(print)) == "https://gaca.example.com"
    out = capsys.readouterr().out
    assert "Tunnels & Mesh" in out and "Published application routes" in out and "HTTP  localhost:41700" in out
    assert "Public hostnames" not in out and "Zero Trust" not in out
    assert "Waiting for your Tunnel to connect" in out and "that is normal" in out


def test_with_no_api_server_the_installer_says_the_app_has_no_crew_yet(flow, capsys):
    flow.hermes_home.mkdir(parents=True)
    (flow.hermes_home / ".env").write_text("OPENAI_API_KEY=keep\n")  # Hermes without its API server switched on
    flow.use_hermes, flow.service = True, "user"

    async def pair_someone():
        await client.connect()
        try:
            await make_owner(name="O", email="o@example.com")
        finally:
            await client.close()
    asyncio.run(pair_someone())
    run_mod.wire_hermes(flow, FakeShell(hermes="v0.21.5"), Say(print))
    out = capsys.readouterr().out
    assert "the app has no crew yet" in out and "crew in the app:" not in out


def test_before_anyone_has_paired_the_installer_does_not_claim_there_is_no_crew(flow, capsys):
    flow.hermes_home.mkdir(parents=True)
    flow.use_hermes = True
    run_mod.wire_hermes(flow, FakeShell(hermes="v0.21.5"), Say(print))
    assert "no crew" not in capsys.readouterr().out




# ── a re-run after the sudo step really does carry on ────────────────

import time as _time


@pytest.fixture
def etc(tmp_path, monkeypatch):
    directory = tmp_path / "etc-systemd"
    directory.mkdir()
    monkeypatch.setattr(run_mod, "SYSTEM_UNIT_DIR", directory)
    return directory


def first_pass_then_the_person_runs_the_sudo_command(flow, etc):
    """Pass one stops for root and leaves the script; the person runs it, which puts that unit in place."""
    flow.service = "system"
    assert run_mod.run(flow, FakeShell(tty=False)) == run_mod.EXIT_NEEDS_ROOT
    script = (flow.config_dir / "install-service.sh").read_text()
    (etc / "gaca-api.service").write_text(script.split("<<'GACA_UNIT'\n", 1)[1].split("GACA_UNIT\n", 1)[0])


def test_after_the_sudo_command_the_rerun_carries_on_without_asking_for_root_again(flow, etc, capsys):
    first_pass_then_the_person_runs_the_sudo_command(flow, etc)
    capsys.readouterr()
    shell = FakeShell(tty=False, started_at=_time.time() + 60)
    assert run_mod.run(flow, shell) == 0
    out = capsys.readouterr().out
    assert "already installed, running and up to date" in out and "Pairing code:" in out  # it got all the way to pairing
    assert not shell.called("sudo") and "One step needs root" not in out


def test_a_service_that_is_not_running_still_needs_root(flow, etc):
    first_pass_then_the_person_runs_the_sudo_command(flow, etc)
    assert run_mod.run(flow, FakeShell(tty=False, started_at=None)) == run_mod.EXIT_NEEDS_ROOT


def test_a_changed_unit_still_needs_root(flow, etc):
    first_pass_then_the_person_runs_the_sudo_command(flow, etc)
    (etc / "gaca-api.service").write_text((etc / "gaca-api.service").read_text() + "# edited\nRestart=never\n")
    assert run_mod.run(flow, FakeShell(tty=False, started_at=_time.time() + 60)) == run_mod.EXIT_NEEDS_ROOT


def test_settings_changed_after_the_service_started_need_a_restart(flow, etc):
    first_pass_then_the_person_runs_the_sudo_command(flow, etc)
    started = (flow.repo_root / ".env").stat().st_mtime - 30  # the service started before .env was last written
    assert run_mod.run(flow, FakeShell(tty=False, started_at=started)) == run_mod.EXIT_NEEDS_ROOT


def test_a_push_key_added_after_the_service_started_needs_a_restart(flow, etc, tmp_path):
    first_pass_then_the_person_runs_the_sudo_command(flow, etc)
    started = _time.time() + 60
    key = flow.config_dir / "secrets" / "fcm-service-account.json"
    key.parent.mkdir(parents=True)
    key.write_text("{}")
    os.utime(key, (started + 30, started + 30))
    assert run_mod.run(flow, FakeShell(tty=False, started_at=started)) == run_mod.EXIT_NEEDS_ROOT


def test_with_passwordless_sudo_an_unchanged_service_is_not_touched(flow, etc):
    first_pass_then_the_person_runs_the_sudo_command(flow, etc)
    shell = FakeShell(sudo_n=True, started_at=_time.time() + 60)
    assert run_mod.run(flow, shell) == 0 and not shell.called("sudo")


def test_service_started_at_reads_systemd_and_refuses_what_is_not_running(monkeypatch):
    from gaca_api.installer.shell import Shell

    class Fake(Shell):
        def __init__(self, shown, state, rc=0):
            self.shown, self.state, self.rc = shown, state, rc

        def run(self, argv, **kwargs):
            if "show" in argv:
                return subprocess.CompletedProcess(argv, 0, self.shown, "")
            return subprocess.CompletedProcess(argv, self.rc, self.state, "")

    monkeypatch.setattr("time.monotonic", lambda: 1000.0)
    monkeypatch.setattr("time.time", lambda: 5000.0)
    assert Fake("940000000\n", "active\n").service_started_at("gaca-api") == pytest.approx(5000.0 - (1000.0 - 940.0))
    assert Fake("940000000\n", "inactive\n", rc=3).service_started_at("gaca-api") is None
    assert Fake("0\n", "active\n").service_started_at("gaca-api") is None
    assert Fake("garbage\n", "active\n").service_started_at("gaca-api") is None
