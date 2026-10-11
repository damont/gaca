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
from . import hermes as hermes_mod
from . import push as push_mod
from . import tunnel as tunnel_mod
from .envconfig import ensure_env
from ..provisioning import hermes_connect
from ..provisioning.envfile import upsert_env
from .privilege import NeedsRoot, run_as_root
from .shell import Shell, user_systemd_env
from .units import render, system_install_script

SYSTEM_UNIT_DIR = Path("/etc/systemd/system")
EXIT_NEEDS_ROOT = 3
EXIT_NEEDS_HERMES = 4
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
    install_hermes: bool = False
    # Push notifications (all optional; see installer/push.py). With none of these and no terminal
    # to ask on, push is simply left off.
    no_push: bool = False
    fcm_credentials: Path | None = None
    apns_key: Path | None = None
    apns_key_id: str | None = None
    apns_team_id: str | None = None
    apns_topic: str | None = None
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


def check_hermes(opts: Options, shell: Shell, say: Say) -> bool:
    """True when Hermes is installed and set up the way GACA needs. Otherwise prints exactly what is
    missing and how to fix it, and returns False. GACA never installs Hermes on its own: with
    --install-hermes (or a yes at the prompt) it runs Hermes's own installer, nothing more."""
    binary = _hermes_bin(shell)
    status = hermes_mod.inspect(shell, opts.hermes_home, binary)
    found = hermes_mod.problems(status, opts.hermes_home)
    if found and binary is None:
        wanted = opts.install_hermes
        if not wanted and shell.is_tty() and not opts.yes:
            say.warn(found[0].what)
            wanted = input(f"Run Hermes's own installer now ({hermes_mod.OFFICIAL_INSTALL})? [y/N] ").strip().lower().startswith("y")
        if wanted:
            say.note("running Hermes's installer (this can take a few minutes)...")
            done = shell.run(["bash", "-c", hermes_mod.OFFICIAL_INSTALL], timeout=1800, inherit=True)
            if done.returncode != 0:
                say.warn("Hermes's installer did not finish; see its output above.")
            binary = _hermes_bin(shell)
            status = hermes_mod.inspect(shell, opts.hermes_home, binary)
            found = hermes_mod.problems(status, opts.hermes_home)
            if not found or binary is not None:
                say.note("Hermes is installed. It still needs `hermes setup` (your model provider) if you have not run it.")
    if not found:
        version = ".".join(map(str, status.version))
        say.ok(f"Hermes {version} is installed and its gateway answers on 127.0.0.1:{status.port}")
        return True
    for problem in found:
        say.warn(problem.what)
        for line in problem.fix:
            say.note(line)
    return False


def install_hermes_skills(opts: Options, say: Say) -> None:
    results = hermes_connect.install_skills(opts.hermes_home)
    for profile, outcome in results.items():
        if isinstance(outcome, str):
            say.warn(f"could not install the GACA skills into Hermes profile {profile!r}: {outcome}")
            continue
        changes = len(outcome.written) + len(outcome.updated)
        if outcome.left_alone:
            say.warn(f"{profile}: {len(outcome.left_alone)} GACA skill file(s) you or an agent edited were left alone")
        if changes:
            say.ok(f"installed the GACA skills into Hermes profile {profile!r}")
        elif not outcome.left_alone:
            say.ok(f"GACA skills already up to date in Hermes profile {profile!r}")


def _ask(prompt: str) -> str:
    return input(prompt).strip()


def _ask_valid(prompt: str, check, say: Say, *, attempts: int = 3):
    """Ask until `check(answer)` accepts it (it returns the cleaned value or raises PushSetupError).
    A blank answer, or too many wrong ones, gives up: None."""
    for _ in range(attempts):
        answer = _ask(prompt)
        if not answer:
            return None
        try:
            return check(answer)
        except push_mod.PushSetupError as error:
            say.warn(str(error))
    return None


def setup_push(opts: Options, shell: Shell, say: Say) -> None:
    """Optional Android (Firebase) and iPhone (APNs) push, from flags or by asking at a terminal."""
    if opts.no_push:
        say.note("push notifications skipped (--no-push)")
        return
    env_file = opts.repo_root / ".env"
    directory = push_mod.secrets_dir(opts.config_dir)
    env = push_mod.read_env(env_file)
    interactive = shell.is_tty() and not opts.yes
    explained = False

    def explain() -> None:
        nonlocal explained
        if not explained:
            for line in push_mod.EXPLAIN:
                say.note(line)
            explained = True

    try:
        # ── Android ──
        if opts.fcm_credentials:
            project = push_mod.setup_android(env_file, directory, opts.fcm_credentials)
            say.ok(f"Android push is set up (Firebase project {project})")
        elif push_mod.android_ready(env):
            say.ok("Android push is already set up")
        elif interactive:
            explain()
            if _ask("Set up push notifications for Android? [y/N] ").lower().startswith("y"):
                say.note("In the Firebase console: Project settings > Service accounts > Generate new private key.")
                say.note("It must be the same Firebase project your app was built with (its google-services.json).")
                project = _ask_valid("Path to the downloaded key file (blank to skip): ",
                                     lambda a: (push_mod.check_fcm(Path(a)), a)[1], say)
                if project:
                    say.ok(f"Android push is set up (Firebase project {push_mod.setup_android(env_file, directory, Path(project))})")
                else:
                    say.note("Android push skipped")

        # ── iPhone ──
        flags = (opts.apns_key, opts.apns_key_id, opts.apns_team_id, opts.apns_topic)
        if any(flags):
            if not all(flags):
                raise RuntimeError("iPhone push needs --apns-key, --apns-key-id, --apns-team-id and --apns-topic together")
            push_mod.setup_ios(env_file, directory, opts.apns_key, opts.apns_key_id, opts.apns_team_id, opts.apns_topic)
            say.ok("iPhone push is set up")
        elif push_mod.ios_ready(env):
            say.ok("iPhone push is already set up")
        elif interactive:
            explain()
            if _ask("Set up push notifications for iPhone? [y/N] ").lower().startswith("y"):
                say.note("From the Apple Developer portal: a Key with the Apple Push Notifications service enabled (a .p8 file),")
                say.note("its Key ID, your Team ID, and the bundle id of the app you built.")
                key = _ask_valid("Path to the .p8 key file (blank to skip): ",
                                 lambda a: (push_mod.check_apns_key(Path(a)), a)[1], say)
                key_id = key and _ask_valid("Key ID (10 characters): ", lambda a: push_mod.check_apple_id(a, "key id"), say)
                team_id = key_id and _ask_valid("Team ID (10 characters): ", lambda a: push_mod.check_apple_id(a, "team id"), say)
                bundle = team_id and _ask_valid("The app's bundle id (like com.example.gaca): ", push_mod.check_bundle_id, say)
                if bundle:
                    push_mod.setup_ios(env_file, directory, Path(key), key_id, team_id, bundle)
                    say.ok("iPhone push is set up")
                else:
                    say.note("iPhone push skipped")
    except push_mod.PushSetupError as error:
        raise RuntimeError(f"push notification setup failed: {error}") from None

    env = push_mod.read_env(env_file)
    if not (push_mod.android_ready(env) or push_mod.ios_ready(env)):
        say.note("push notifications are off: alerts show in the app when it is open. To add push later, run ./install.sh again")


_PATH_MARK = "# gaca installer: put ~/.local/bin on the PATH (where the `gaca-api` command lives)"


def ensure_on_path(say: Say) -> None:
    """Make sure new shells find ~/.local/bin: append one guarded line to the login shell's startup file.

    Only appends (nothing existing is rewritten), only once, and never through a symlink (a dotfile
    manager's file is the user's to edit: the line is printed instead). If the file already mentions
    ~/.local/bin (the uv installer, Ubuntu's defaults and many people's own setup do), it is left alone.
    """
    home = Path.home()
    shell_name = Path(os.environ.get("SHELL", "")).name
    if shell_name == "fish":
        rc, line = home / ".config" / "fish" / "config.fish", "fish_add_path -g $HOME/.local/bin"
    else:
        name = ".zshrc" if shell_name == "zsh" else ".bashrc" if shell_name == "bash" else ".profile"
        rc = home / name
        line = 'case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) export PATH="$HOME/.local/bin:$PATH" ;; esac'
    existing = rc.read_text(errors="replace") if rc.exists() else ""
    if _PATH_MARK in existing or ".local/bin" in existing:
        say.ok(f"~/.local/bin is already set up on your PATH ({rc})")
        return
    if rc.is_symlink():
        say.warn(f"{rc} is a symlink, so it was not edited. Add this line to it yourself:")
        say.note(f"    {line}")
        return
    rc.parent.mkdir(parents=True, exist_ok=True)
    with open(rc, "a") as handle:
        handle.write(f"\n{_PATH_MARK}\n{line}\n")
    say.ok(f"added ~/.local/bin to your PATH in {rc}")
    say.note(f"open a new shell (or run `source {rc}`) and `gaca-api` will be found")


def link_cli(opts: Options, say: Say) -> None:
    """Put `gaca-api` on the PATH (~/.local/bin) so every command works from any directory."""
    target = opts.repo_root / "gaca-api" / ".venv" / "bin" / "gaca-api"
    if not target.exists():
        return
    bin_dir = Path.home() / ".local" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    link = bin_dir / "gaca-api"
    if link.is_symlink() and Path(os.readlink(link)) == target:
        pass
    elif link.exists() or link.is_symlink():
        say.warn(f"{link} already exists and is not this install's, so it was left alone; use {target} instead")
        return
    else:
        link.symlink_to(target)
    say.ok(f"`gaca-api` is available as {link}")
    ensure_on_path(say)


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
In the Cloudflare dashboard: Networks > Tunnels & Mesh > create a tunnel (Cloudflared).
  1. Copy the token from the install command it shows (the long string after `--token`); do not run that command.
  2. On the tunnel's "Published application routes" tab, add your hostname with service  HTTP  localhost:{opts.port}
Cloudflare's page says "Waiting for your Tunnel to connect" until this installer has started the tunnel: that is normal.""")
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


PAIR_CODE_MINUTES = 30


def print_setup_info(say: Say, base_url: str | None, code: str, port: int) -> None:
    import segno

    shown = base_url or f"http://127.0.0.1:{port}"
    link = "gaca://setup?" + urlencode({"url": shown, "code": code.replace("-", "")})
    say.out("")
    say.out(f"  Server address:  {shown}")
    say.out(f"  Pairing code:    {code}   (single use, valid {PAIR_CODE_MINUTES} minutes)")
    say.out("")
    segno.make(link, error="m").terminal(compact=True)
    say.out("")
    say.out("  On your phone, open the GACA app and scan this (or choose \"Enter a code instead\" and type")
    say.out("  the address and code). To pair another phone later, run:  gaca-api pair")


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


async def _issue_code(opts: Options) -> str:
    from ..db import client
    from ..services import pairing as pairing_service

    settings.db_path = str(opts.data_dir / "gaca.db")
    await client.connect()
    try:
        code, _ = await pairing_service.issue_code(PAIR_CODE_MINUTES)
        return code
    finally:
        await client.close()


def run(opts: Options, shell: Shell | None = None, out=print) -> int:
    shell, say = shell or Shell(), Say(out)
    opts = _paths(opts)
    try:
        say.step("Hermes")
        if opts.use_hermes:
            if not check_hermes(opts, shell, say):
                say.note("GACA needs Hermes working first. (To set up only the server for now, add --no-hermes.)")
                return EXIT_NEEDS_HERMES
            install_hermes_skills(opts, say)
        else:
            say.warn("skipping the Hermes checks (--no-hermes). Without Hermes the app has no agent to talk to.")

        say.step("Settings")
        for note in ensure_env(opts.repo_root / ".env", data_dir=opts.data_dir, port=opts.port,
                               hermes_home=opts.hermes_home, hermes_bin=_hermes_bin(shell)):
            say.ok(note)
        opts.data_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(opts.data_dir, 0o700)

        say.step("Push notifications")
        setup_push(opts, shell, say)

        say.step("Service")
        mode = choose_service(opts, shell)
        install_service(opts, shell, say, mode)
        if not wait_healthy(shell, opts.port):
            raise RuntimeError(f"the server did not come up on port {opts.port}; check `journalctl -u {opts.name}` (or `journalctl --user -u {opts.name}`)")
        say.ok(f"server is healthy on 127.0.0.1:{opts.port}")

        say.step("Internet access")
        base_url = setup_tunnel(opts, shell, say)

        if base_url:
            # So `gaca-api pair` can put the address in its QR code later.
            upsert_env(opts.repo_root / ".env", {"GACA_PUBLIC_URL": base_url})

        say.step("Your phone")
        claimed = server_claimed(shell, opts.port)
        if claimed:
            say.ok("a phone is already paired with this server; to pair another, run: gaca-api pair")
            say.step("Hermes")
            wire_hermes(opts, shell, say)
            return 0
        code = asyncio.run(_issue_code(opts))
        print_setup_info(say, base_url, code, opts.port)

        if opts.wait_for_claim and opts.use_hermes and opts.hermes_home.is_dir():
            say.step("Waiting for you to pair your phone (Ctrl-C to skip; then run `gaca-api hermes connect` later)")
            waited = 0
            try:
                while waited < opts.claim_wait_seconds:
                    if server_claimed(shell, opts.port):
                        say.ok("phone paired")
                        wire_hermes(opts, shell, say)
                        break
                    shell.sleep(3)
                    waited += 3
                else:
                    say.warn("timed out waiting. Once your phone is paired, run: gaca-api hermes connect")
            except KeyboardInterrupt:
                say.note("skipped. Once your phone is paired, run: gaca-api hermes connect")
        else:
            say.note("after your phone is paired, run `gaca-api hermes connect` if you use Hermes on this machine")
        say.step("Done")
        link_cli(opts, say)
        say.note("check:   gaca-api doctor       (is everything working, and how to fix what is not)")
        say.note("phones:  gaca-api pair         (add another phone)    gaca-api device list|revoke")
        say.note("push:    gaca-api push android|ios   (set up or change push notifications)")
        say.note("service: gaca-api service status|restart|logs      backup: gaca-api backup")
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

        link = Path.home() / ".local" / "bin" / "gaca-api"
        if link.is_symlink() and Path(os.readlink(link)) == opts.repo_root / "gaca-api" / ".venv" / "bin" / "gaca-api":
            link.unlink()
            say.ok("removed the `gaca-api` command from ~/.local/bin")

        if purge:
            say.step("Data")
            import shutil

            if opts.data_dir.exists():
                shutil.rmtree(opts.data_dir)
                say.ok(f"deleted {opts.data_dir} (the database, with your chats and paired phones)")
            secrets = push_mod.secrets_dir(opts.config_dir)
            if secrets.exists():
                shutil.rmtree(secrets)
                say.ok(f"deleted {secrets} (push notification keys)")
            env_file = opts.repo_root / ".env"
            if env_file.exists():
                env_file.unlink()
                say.ok("deleted .env")
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
