"""The installer's steps, in order. Each is idempotent, so a re-run resumes."""
from __future__ import annotations

import asyncio
import getpass
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

from ..config import settings
from . import tunnel as tunnel_mod
from .envconfig import ensure_env
from .privilege import NeedsRoot, run_as_root
from .shell import Shell, user_systemd_env
from .units import render, system_install_script

SYSTEM_UNIT_DIR = Path("/etc/systemd/system")
EXIT_NEEDS_ROOT = 3
EXIT_FAILED = 1


@dataclass
class Options:
    repo_root: Path
    service: str = "auto"  # auto | system | user
    name: str = "gaca-api"
    data_dir: Path | None = None
    port: int = 41700
    hostname: str | None = None
    tunnel: str = "ask"  # ask | token | quick | none
    tunnel_token_file: Path | None = None
    hermes_home: Path | None = None
    use_hermes: bool = True
    config_dir: Path | None = None
    wait_for_claim: bool = True
    claim_wait_seconds: int = 3600
    yes: bool = False


class Say:
    def __init__(self, out=print):
        self.out = out

    def step(self, text): self.out(f"\n==> {text}")
    def ok(self, text): self.out(f"    ✓ {text}")
    def note(self, text): self.out(f"    {text}")
    def warn(self, text): self.out(f"    ! {text}")


def _paths(opts: Options) -> Options:
    home = Path.home()
    opts.data_dir = opts.data_dir or home / "gaca-data"
    opts.hermes_home = opts.hermes_home or home / ".hermes"
    opts.config_dir = opts.config_dir or home / ".config" / "gaca"
    return opts


def _hermes_bin(shell: Shell) -> str | None:
    found = shell.which("hermes")
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / "hermes"
    return str(fallback) if fallback.exists() else None


def choose_service(opts: Options, shell: Shell) -> str:
    if opts.service in ("system", "user"):
        return opts.service
    if shell.euid() == 0 or shell.run(["sudo", "-n", "true"]).returncode == 0 or shell.is_tty():
        return "system"
    raise RuntimeError(
        "no root access here (no passwordless sudo, and no terminal for sudo to ask on). Choose: "
        "`--service system` (recommended; needs sudo, sandboxes the server) or `--service user` "
        "(no root; the server runs as you with no filesystem sandbox)."
    )


def install_service(opts: Options, shell: Shell, say: Say, mode: str) -> None:
    deploy = opts.repo_root / "deploy"
    values = dict(
        USER=getpass.getuser(), HOME=str(Path.home()), REPO=str(opts.repo_root),
        DATA_DIR=str(opts.data_dir), HERMES_HOME=str(opts.hermes_home), CONFIG_DIR=str(opts.config_dir),
    )
    if mode == "system":
        unit = render((deploy / "gaca-api.service").read_text(), **values)
        script = opts.config_dir / "install-service.sh"
        run_as_root(shell, system_install_script(unit, opts.name), script)
        say.ok(f"system service {opts.name} installed and started (sandboxed)")
        return
    unit = render((deploy / "gaca-api.user.service").read_text(), **values)
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    (unit_dir / f"{opts.name}.service").write_text(unit)
    env = user_systemd_env()
    for argv in (["daemon-reload"], ["enable", f"{opts.name}.service"], ["restart", f"{opts.name}.service"]):
        result = shell.run(["systemctl", "--user", *argv], env=env)
        if result.returncode != 0:
            raise RuntimeError(f"`systemctl --user {' '.join(argv)}` failed: {(result.stderr or '').strip()}")
    linger = shell.run(["loginctl", "enable-linger", getpass.getuser()])
    if linger.returncode != 0:
        say.warn("could not enable lingering: the server will stop when you log out (run `sudo loginctl enable-linger $USER` once to keep it running)")
    say.ok(f"user service {opts.name} installed and started")
    say.warn("per-user service: runs as you with no filesystem sandbox. Use --service system on a machine where you have sudo.")


def wait_healthy(shell: Shell, port: int, *, wait: float = 40) -> bool:
    waited = 0.0
    while waited <= wait:
        status, body = shell.http_get(f"http://127.0.0.1:{port}/api/health", timeout=3)
        if status == 200 and '"gaca-api"' in body:
            return True
        shell.sleep(2)
        waited += 2
    return False


def server_claimed(shell: Shell, port: int) -> bool | None:
    status, body = shell.http_get(f"http://127.0.0.1:{port}/api/setup", timeout=5)
    if status != 200:
        return None
    try:
        return bool(json.loads(body)["claimed"])
    except (ValueError, KeyError):
        return None


def _prompt_tunnel(opts: Options, say: Say) -> str:
    if opts.tunnel != "ask":
        return opts.tunnel
    if opts.tunnel_token_file or os.environ.get("GACA_TUNNEL_TOKEN"):
        return "token"
    if opts.yes or not sys.stdin.isatty():
        return "none"
    say.out("""
How should your phone reach this server?
  1) Cloudflare Tunnel with my own hostname (recommended). You need a tunnel token.
  2) A temporary link, no account needed (try it out; the address changes if it restarts).
  3) Not now (reachable on this machine only).""")
    return {"1": "token", "2": "quick", "3": "none"}.get(input("Choose 1, 2 or 3 [1]: ").strip() or "1", "none")


def setup_tunnel(opts: Options, shell: Shell, say: Say) -> str | None:
    """Returns the server's public base URL, or None if it is not exposed."""
    kind = _prompt_tunnel(opts, say)
    if kind == "none":
        say.note("skipping the tunnel; the server answers on this machine only (127.0.0.1:%d)" % opts.port)
        return None
    if not tunnel_mod.docker_ready(shell):
        say.warn("Docker is not available to this user, so cloudflared can't be started.")
        say.note("Install Docker and make sure `docker info` works for you, then re-run ./install.sh.")
        return None
    if kind == "quick":
        url = tunnel_mod.start_quick_tunnel(shell, opts.port)
        say.ok(f"temporary tunnel is up: {url}")
        say.note("this address is temporary: it changes if the tunnel restarts. Use a tunnel token for a permanent one.")
        return url
    token = None
    if opts.tunnel_token_file:
        token = opts.tunnel_token_file.read_text()
    elif os.environ.get("GACA_TUNNEL_TOKEN"):
        token = os.environ["GACA_TUNNEL_TOKEN"]
    elif sys.stdin.isatty():
        say.out(f"""
In the Cloudflare dashboard: Zero Trust > Networks > Tunnels > Create a tunnel > Cloudflared.
  1. Copy the token from the install command it shows (the long string after `--token`).
  2. Under "Public hostnames" add your hostname with service  HTTP  localhost:{opts.port}""")
        token = getpass.getpass("Paste the tunnel token (input is hidden): ")
    if not token:
        raise RuntimeError("tunnel mode 'token' needs --tunnel-token-file, GACA_TUNNEL_TOKEN, or a terminal to paste into")
    hostname = opts.hostname or (input("Public hostname (e.g. gaca.example.com): ") if sys.stdin.isatty() else None)
    if not hostname:
        raise RuntimeError("tunnel mode 'token' needs --hostname")
    host = tunnel_mod.normalize_hostname(hostname)
    compose = tunnel_mod.write_token_setup(opts.config_dir / "tunnel", tunnel_mod.valid_token(token))
    tunnel_mod.start_token_tunnel(shell, compose)
    say.ok("cloudflared started (token stored 0600 in %s)" % compose.parent)
    ok, status = tunnel_mod.wait_public(shell, host)
    if ok:
        say.ok(f"https://{host} reaches your server")
    else:
        say.warn(f"https://{host}/api/health did not answer correctly (last status {status}). Check that the tunnel's public hostname uses service HTTP localhost:{opts.port}, then try the URL again.")
    return f"https://{host}"


def print_setup_info(say: Say, base_url: str | None, code: str, port: int) -> None:
    import segno

    shown = base_url or f"http://127.0.0.1:{port}"
    link = "gaca://setup?" + urlencode({"url": shown, "code": code.replace("-", "")})
    say.out("")
    say.out(f"  Server address:  {shown}")
    say.out(f"  Setup code:      {code}   (single use, valid 60 minutes)")
    say.out("")
    segno.make(link, error="m").terminal(compact=True)
    say.out("")
    say.out("  On your phone, open the GACA app, scan this (or enter the address and code), then")
    say.out('  choose "Set up this server" and create your account. The code is only for this first step.')


async def _wire_hermes(opts: Options) -> list:
    from ..db import client
    from ..db import users as users_db
    from ..provisioning import hermes_connect

    settings.db_path = str(opts.data_dir / "gaca.db")
    settings.hermes_home = str(opts.hermes_home)
    settings.api_port = opts.port
    await client.connect()
    try:
        owner = await users_db.first()
        if owner is None:
            return []
        return await hermes_connect.connect_profiles(opts.hermes_home, hermes_connect.gaca_base_url(), owner)
    finally:
        await client.close()


def wire_hermes(opts: Options, shell: Shell, say: Say) -> None:
    if not opts.use_hermes:
        return
    if not opts.hermes_home.is_dir():
        say.note(f"no Hermes found at {opts.hermes_home}; skipping. Install Hermes, then run: gaca-api hermes connect")
        return
    wired = asyncio.run(_wire_hermes(opts))
    fresh = [c for c in wired if c.token_id is not None]
    if fresh:
        say.ok(f"gave {len(fresh)} Hermes profile(s) a scoped GACA token: {', '.join(c.profile for c in fresh)}")
        say.warn("restart Hermes so running agents pick it up (e.g. `systemctl --user restart hermes-gateway`)")
    else:
        say.ok("every Hermes profile already has a GACA token")


async def _issue_code(opts: Options) -> str | None:
    from ..db import client
    from ..services import setup as setup_service

    settings.db_path = str(opts.data_dir / "gaca.db")
    await client.connect()
    try:
        try:
            code, _ = await setup_service.issue_code()
        except setup_service.SetupRefused:
            return None
        return code
    finally:
        await client.close()


def run(opts: Options, shell: Shell | None = None, out=print) -> int:
    shell, say = shell or Shell(), Say(out)
    opts = _paths(opts)
    try:
        say.step("Settings")
        for note in ensure_env(opts.repo_root / ".env", data_dir=opts.data_dir, port=opts.port,
                               hermes_home=opts.hermes_home, hermes_bin=_hermes_bin(shell)):
            say.ok(note)
        opts.data_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(opts.data_dir, 0o700)

        say.step("Service")
        mode = choose_service(opts, shell)
        install_service(opts, shell, say, mode)
        if not wait_healthy(shell, opts.port):
            raise RuntimeError(f"the server did not come up on port {opts.port}; check `journalctl -u {opts.name}` (or `journalctl --user -u {opts.name}`)")
        say.ok(f"server is healthy on 127.0.0.1:{opts.port}")

        say.step("Internet access")
        base_url = setup_tunnel(opts, shell, say)

        say.step("Your account")
        claimed = server_claimed(shell, opts.port)
        if claimed:
            say.ok("this server already has an owner: open the app and sign in")
            say.step("Hermes")
            wire_hermes(opts, shell, say)
            return 0
        code = asyncio.run(_issue_code(opts))
        if code is None:
            say.ok("this server already has an owner: open the app and sign in")
            return 0
        print_setup_info(say, base_url, code, opts.port)

        if opts.wait_for_claim and opts.use_hermes and opts.hermes_home.is_dir():
            say.step("Waiting for you to finish in the app (Ctrl-C to skip; then run `gaca-api hermes connect` later)")
            waited = 0
            try:
                while waited < opts.claim_wait_seconds:
                    if server_claimed(shell, opts.port):
                        say.ok("account created")
                        wire_hermes(opts, shell, say)
                        break
                    shell.sleep(3)
                    waited += 3
                else:
                    say.warn("timed out waiting. Once you have signed in, run: gaca-api hermes connect")
            except KeyboardInterrupt:
                say.note("skipped. Once you have signed in, run: gaca-api hermes connect")
        else:
            say.note("after you have signed in, run `gaca-api hermes connect` if you use Hermes on this machine")
        say.step("Done")
        say.note(f"logs:    journalctl -u {opts.name}" + ("  (--user)" if mode == "user" else ""))
        say.note("update:  git pull && ./install.sh   (keeps your secrets and data)")
        return 0
    except NeedsRoot as needed:
        say.step("One step needs root")
        say.out(f"    I can't get root here. Run this one command, then run ./install.sh again to continue:\n\n        sudo bash {needed.script}\n")
        return EXIT_NEEDS_ROOT
    except (RuntimeError, ValueError, tunnel_mod.TunnelError) as error:
        say.out(f"\nInstall stopped: {error}")
        return EXIT_FAILED


# ── uninstall ────────────────────────────────────────────────────────

def uninstall(opts: Options, shell: Shell | None = None, out=print, *, purge: bool = False) -> int:
    """Stop and remove the service and the tunnel. Keeps your data and secrets unless `purge`.
    Hermes' files are not touched (tokens stay valid until revoked: `gaca-api token list`)."""
    shell, say = shell or Shell(), Say(out)
    opts = _paths(opts)
    try:
        say.step("Service")
        user_unit = Path.home() / ".config" / "systemd" / "user" / f"{opts.name}.service"
        system_unit = SYSTEM_UNIT_DIR / f"{opts.name}.service"
        found_a_service = user_unit.exists() or system_unit.exists()
        if user_unit.exists():
            env = user_systemd_env()
            shell.run(["systemctl", "--user", "disable", "--now", f"{opts.name}.service"], env=env)
            user_unit.unlink()
            shell.run(["systemctl", "--user", "daemon-reload"], env=env)
            say.ok("user service stopped and removed")
        if system_unit.exists():
            script = f"""#!/usr/bin/env bash
# Generated by gaca-api uninstall. Stops and removes the {opts.name} system service.
set -euo pipefail
systemctl disable --now {opts.name} || true
rm -f /etc/systemd/system/{opts.name}.service
systemctl daemon-reload
echo "{opts.name} removed"
"""
            run_as_root(shell, script, opts.config_dir / "uninstall-service.sh")
            say.ok("system service stopped and removed")
        if not found_a_service:
            say.note("no service found (already removed?)")

        say.step("Tunnel")
        if shell.which("docker"):
            shell.run(["docker", "rm", "-f", tunnel_mod.QUICK_NAME], timeout=60)
            compose = opts.config_dir / "tunnel" / "compose.yml"
            if compose.exists():
                shell.run(["docker", "compose", "-f", str(compose), "down"], timeout=120)
                for leftover in compose.parent.iterdir():
                    leftover.unlink()
                compose.parent.rmdir()
                say.ok("cloudflared stopped; the stored tunnel token was deleted")
            else:
                say.note("no tunnel configuration found")
        else:
            say.note("Docker is not available here; if a tunnel container is running, remove it yourself")

        if purge:
            say.step("Data")
            import shutil

            if opts.data_dir.exists():
                shutil.rmtree(opts.data_dir)
                say.ok(f"deleted {opts.data_dir} (the database, with your chats and account)")
            env_file = opts.repo_root / ".env"
            if env_file.exists():
                env_file.unlink()
                say.ok("deleted .env (including the JWT secret)")
        else:
            say.step("Kept")
            say.note(f"your data ({opts.data_dir}) and settings ({opts.repo_root / '.env'}) are untouched; add --purge to delete them")
        return 0
    except NeedsRoot as needed:
        say.out(f"\n    I can't get root here. Run this one command, then run uninstall again:\n\n        sudo bash {needed.script}\n")
        return EXIT_NEEDS_ROOT
    except (RuntimeError, ValueError) as error:
        say.out(f"\nUninstall stopped: {error}")
        return EXIT_FAILED
