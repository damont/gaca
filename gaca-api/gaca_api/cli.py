"""The `gaca-api` command. With no arguments it starts the server (what the systemd unit runs).

Owner-only operations live here rather than behind HTTP because having a shell on the
server *is* the proof of ownership: pairing a phone, unpairing one, minting Hermes tokens.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from urllib.parse import urlencode

from .config import settings
from .db import client
from .db import users as users_db
from .models.base import utcnow
from .provisioning import hermes_connect
from .services import pairing as pairing_service
from .services import tokens as token_service
from .services.exceptions import InvalidRequest, NotFound
from .utils import tokens as token_utils


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gaca-api", description="GACA API server and owner tools")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="start the server (the default)")
    pair = sub.add_parser("pair", help="print a one-time code and QR code to pair a phone with this server")
    pair.add_argument("--ttl", type=int, default=pairing_service.DEFAULT_TTL_MINUTES, metavar="MINUTES")
    pair.add_argument("--url", help="the server's public https URL (default: GACA_PUBLIC_URL)")
    pair.add_argument("--no-qr", action="store_true", help="print the code and link only")
    device = sub.add_parser("device", help="manage paired phones").add_subparsers(dest="device_command", required=True)
    device.add_parser("list", help="list paired devices")
    unpair = device.add_parser("revoke", help="unpair a device by id")
    unpair.add_argument("device_id")
    sub.add_parser("status", help="does this server have an owner yet, and how many phones are paired?")

    def named(parser):
        parser.add_argument("--name", default="gaca-api", help="service name (default: gaca-api)")
        return parser

    named(sub.add_parser("doctor", help="check that everything is working and say how to fix what is not"))
    push = sub.add_parser("push", help="push notifications: set up Android or iPhone after installing, or turn one off").add_subparsers(dest="push_command", required=True)
    push.add_parser("status", help="which push channels are on")
    android = named(push.add_parser("android", help="set up (or replace) Android push with a Firebase service-account key"))
    android.add_argument("--key", metavar="FILE", help="the key file (asked for at a terminal if omitted)")
    android.add_argument("--restart", action="store_true", help="restart the server afterwards")
    ios = named(push.add_parser("ios", help="set up (or replace) iPhone push with an Apple APNs key"))
    ios.add_argument("--key", metavar="FILE", help="the .p8 key file")
    ios.add_argument("--key-id", metavar="ID")
    ios.add_argument("--team-id", metavar="ID")
    ios.add_argument("--bundle-id", metavar="BUNDLE_ID", help="the app's bundle id")
    ios.add_argument("--restart", action="store_true", help="restart the server afterwards")
    remove = named(push.add_parser("remove", help="turn one platform's push off and delete its key"))
    remove.add_argument("platform", choices=["android", "ios"])
    remove.add_argument("--restart", action="store_true", help="restart the server afterwards")
    service = sub.add_parser("service", help="the systemd service: status, restart, logs").add_subparsers(dest="service_command", required=True)
    named(service.add_parser("status", help="is it running and answering?"))
    named(service.add_parser("restart", help="restart it (asks for sudo if it is a system service)"))
    logs = named(service.add_parser("logs", help="show the server's log"))
    logs.add_argument("-f", "--follow", action="store_true", help="keep following new lines (Ctrl-C to stop)")
    logs.add_argument("-n", "--lines", type=int, default=100, help="how many recent lines (default 100)")
    backup = sub.add_parser("backup", help="save a consistent copy of the database (chats, paired phones) to a private file")
    backup.add_argument("file", nargs="?", help="where to write it (default: ~/gaca-backups/gaca-<time>.db)")
    url = sub.add_parser("public-url", help="show or set the address phones are told to use (goes in the pairing QR code)")
    url.add_argument("url", nargs="?", help="e.g. https://gaca.example.com; omit to show the current one")

    uninstall = sub.add_parser("uninstall", help="stop and remove the service and tunnel (keeps your data unless --purge)")
    uninstall.add_argument("--repo-root", required=True)
    uninstall.add_argument("--name", default="gaca-api")
    uninstall.add_argument("--data-dir", default=None)
    uninstall.add_argument("--purge", action="store_true", help="ALSO delete the database and .env (irreversible)")
    uninstall.add_argument("--yes", action="store_true", help="do not ask for confirmation of --purge")

    install = sub.add_parser("install", help="set this server up (normally run via ./install.sh)")
    install.add_argument("--repo-root", required=True)
    install.add_argument("--service", choices=["auto", "system", "user"], default="auto", help="system: sandboxed, needs sudo (recommended). user: no root, no sandbox")
    install.add_argument("--name", default="gaca-api", help="service name")
    install.add_argument("--data-dir", default=None, help="default: ~/gaca-data")
    install.add_argument("--port", type=int, default=41700)
    install.add_argument("--tunnel", choices=["ask", "token", "quick", "none"], default="ask", help="how to reach the server from the internet")
    install.add_argument("--hostname", default=None, help="the tunnel's public hostname, e.g. gaca.example.com")
    install.add_argument("--tunnel-token-file", default=None, help="file holding the Cloudflare tunnel token (or set GACA_TUNNEL_TOKEN)")
    install.add_argument("--hermes-home", default=None, help="default: ~/.hermes")
    install.add_argument("--no-hermes", action="store_true", help="skip the Hermes requirements check (the server alone is useless to the app: it needs an agent)")
    install.add_argument("--install-hermes", action="store_true", help="if Hermes is missing, run its official installer (you still run `hermes setup` yourself)")
    install.add_argument("--no-push", action="store_true", help="do not ask about push notifications")
    install.add_argument("--fcm-credentials", metavar="FILE", help="Android push: your Firebase service-account key (JSON)")
    install.add_argument("--apns-key", metavar="FILE", help="iPhone push: your Apple APNs key (.p8); also needs the next three")
    install.add_argument("--apns-key-id", metavar="ID", help="iPhone push: the key's 10-character Key ID")
    install.add_argument("--apns-team-id", metavar="ID", help="iPhone push: your 10-character Apple Team ID")
    install.add_argument("--apns-topic", metavar="BUNDLE_ID", help="iPhone push: the app's bundle id")
    install.add_argument("--no-wait", action="store_true", help="do not wait for you to pair your phone")
    install.add_argument("--yes", action="store_true", help="never ask questions")

    token = sub.add_parser("token", help="manage API tokens (what Hermes skills use)").add_subparsers(dest="token_command", required=True)
    create = token.add_parser("create", help="mint a token; the secret is printed once")
    create.add_argument("--name", required=True)
    create.add_argument("--scopes", default=",".join(token_utils.SCOPES), help="comma-separated: " + ", ".join(token_utils.SCOPES))
    create.add_argument("--expires-days", type=int, default=None, help="default: never expires")
    token.add_parser("list", help="list tokens (never shows secrets)")
    revoke = token.add_parser("revoke", help="revoke a token by id")
    revoke.add_argument("token_id")

    hermes = sub.add_parser("hermes", help="wire Hermes to this server").add_subparsers(dest="hermes_command", required=True)
    connect = hermes.add_parser("connect", help="mint a token per Hermes profile and write it into the profile's .env")
    connect.add_argument("--hermes-home", default=None, help="default: GACA_HERMES_HOME or ~/.hermes")
    connect.add_argument("--base-url", default=None, help="default: http://127.0.0.1:<api port>")
    connect.add_argument("--rotate", action="store_true", help="replace tokens already in place (default: only wire profiles that have none)")
    return parser


async def _pair(ttl: int, url: str | None, show_qr: bool) -> int:
    code, _ = await pairing_service.issue_code(ttl)
    server = (url or settings.public_url).strip().rstrip("/")
    print(f"Pairing code:  {code}   (single use, valid {ttl} minutes)")
    if not server:
        print(
            "This server doesn't know its public address, so there is no QR code. In the app choose\n"
            "\"Enter code instead\", type the address and this code. To get QR codes, set GACA_PUBLIC_URL\n"
            "in the server's .env (or pass --url).",
            file=sys.stderr,
        )
        return 0
    link = f"gaca://setup?{urlencode({'url': server, 'code': code.replace('-', '')})}"
    if show_qr:
        import segno

        segno.make(link, error="m").terminal(compact=True)
        print("Open GACA on your phone and scan this code.")
    print(f"Or open this link on the phone:  {link}")
    return 0


async def _device_list() -> int:
    owner = await _owner_or_none()
    if owner is None:
        return 1
    devices = await token_service.list_devices(owner)
    if not devices:
        print("No paired devices. Run `gaca-api pair`.")
    for d in devices:
        used = d.last_used_at.isoformat(timespec="seconds") if d.last_used_at else "never"
        print(f"{d.id}  {d.name:24}  paired {d.created_at.date()}  last used: {used}")
    return 0


async def _device_revoke(device_id: str) -> int:
    owner = await _owner_or_none()
    if owner is None:
        return 1
    if device_id not in {d.id for d in await token_service.list_devices(owner)}:
        print(f"No paired device {device_id!r}.", file=sys.stderr)
        return 1
    await token_service.revoke(owner, device_id)
    print(f"Unpaired {device_id}.")
    return 0


async def _status() -> int:
    _push_status()
    owner = await users_db.first()
    if owner is None:
        print("no owner yet — run `gaca-api pair` and open the app")
        return 0
    n = len(await token_service.list_devices(owner))
    print(f"paired devices: {n}" + ("" if n else " — run `gaca-api pair`"))
    return 0


def _push_status() -> None:
    from pathlib import Path as _Path

    android = bool(settings.fcm_credentials_path) and _Path(settings.fcm_credentials_path).is_file()
    iphone = bool(settings.apns_key_path and settings.apns_key_id and settings.apns_team_id) and _Path(settings.apns_key_path).is_file()
    print(f"push notifications: Android {'on' if android else 'off'}, iPhone {'on' if iphone else 'off'}"
          + ("" if android or iphone else " (re-run ./install.sh to set them up)"))


async def _owner_or_none(next_step: str = "pair"):
    owner = await users_db.first()
    if owner is None:
        print(f"This server has no owner yet; use `gaca-api {next_step}`.", file=sys.stderr)
    return owner


async def _token_create(name: str, scopes: str, expires_days: int | None) -> int:
    owner = await _owner_or_none()
    if owner is None:
        return 1
    try:
        doc, secret = await token_service.create(owner, name, [s.strip() for s in scopes.split(",") if s.strip()], expires_days)
    except InvalidRequest as error:
        print(error, file=sys.stderr)
        return 1
    print(f"Token {doc.id} ({doc.name}; scopes: {', '.join(doc.scopes)})")
    print(f"Secret (shown once, store it now):  {secret}")
    return 0


async def _token_list() -> int:
    owner = await _owner_or_none()
    if owner is None:
        return 1
    for t in await token_service.list_tokens(owner):
        state = "revoked" if t.revoked_at else ("expired" if t.expires_at and t.expires_at <= utcnow() else "active")
        used = t.last_used_at.isoformat(timespec="seconds") if t.last_used_at else "never"
        print(f"{t.id}  {state:8}  {t.name:20}  scopes={','.join(t.scopes)}  last used: {used}")
    return 0


async def _token_revoke(token_id: str) -> int:
    owner = await _owner_or_none()
    if owner is None:
        return 1
    try:
        await token_service.revoke(owner, token_id)
    except NotFound:
        print(f"No token {token_id!r}.", file=sys.stderr)
        return 1
    print(f"Revoked {token_id}.")
    return 0


async def _hermes_connect(hermes_home: str | None, base_url: str | None, rotate: bool = False) -> int:
    owner = await _owner_or_none()
    if owner is None:
        return 1
    home = Path(hermes_home or settings.hermes_home).expanduser()
    if not home.is_dir():
        print(f"No Hermes home at {home}. Pass --hermes-home.", file=sys.stderr)
        return 1
    url = base_url or f"http://127.0.0.1:{settings.api_port}"
    connected = await hermes_connect.connect_profiles(home, url, owner, rotate=rotate)
    wired = [c for c in connected if c.token_id is not None]
    for c in connected:
        note = "already has a GACA token, left alone" if c.token_id is None else f"token {c.token_name}"
        print(f"  {c.profile:12} {note}  ({c.env_path})")
    if wired:
        print(f"Wrote GACA_BASE_URL={url} and a scoped GACA_API_TOKEN into {len(wired)} profile(s).")
        print("Restart Hermes so running agents pick up the new token (e.g. `systemctl --user restart hermes-gateway`).")
    else:
        print("Every profile already has a GACA token; nothing to do. (`--rotate` replaces them.)")
    print("GACA skills:")
    for profile, outcome in hermes_connect.install_skills(home).items():
        if isinstance(outcome, str):
            print(f"  {profile:12} NOT installed: {outcome}")
            continue
        changes = len(outcome.written) + len(outcome.updated)
        note = f"{changes} file(s) installed or updated" if changes else "up to date"
        if outcome.left_alone:
            note += f"; {len(outcome.left_alone)} you or an agent edited were left alone ({', '.join(outcome.left_alone)})"
        print(f"  {profile:12} {note}")
    return 0


async def _with_database(coro_factory) -> int:
    # The owner tools act on the real server's database, never on a new one: with the wrong
    # (or unset) GACA_DB_PATH, connect() would quietly create an empty file and these commands
    # would report on, and issue codes for, a server that does not exist.
    if not Path(settings.db_path).is_file():
        print(
            f"No database at {settings.db_path}. Start the server once, or set GACA_DB_PATH "
            "to the server's database (it is in the server's .env).",
            file=sys.stderr,
        )
        return 2
    print(f"Using database {Path(settings.db_path).resolve()}", file=sys.stderr)
    await client.connect()
    try:
        return await coro_factory()
    finally:
        await client.close()


def _uninstall(args) -> int:
    from .installer.run import Options, uninstall

    if args.purge and not args.yes:
        if not sys.stdin.isatty() or input("This permanently deletes the database (chats, account) and .env. Type DELETE to confirm: ").strip() != "DELETE":
            print("Not purging. (Use --yes to skip this question.)", file=sys.stderr)
            return 1
    data_dir = Path(args.data_dir).expanduser() if args.data_dir else None
    return uninstall(Options(repo_root=Path(args.repo_root).resolve(), name=args.name, data_dir=data_dir), purge=args.purge)


def _install(args) -> int:
    from .installer.run import Options, run

    def maybe(value):
        return Path(value).expanduser() if value else None

    return run(Options(
        repo_root=Path(args.repo_root).resolve(), service=args.service, name=args.name,
        data_dir=maybe(args.data_dir), port=args.port, hostname=args.hostname, tunnel=args.tunnel,
        tunnel_token_file=maybe(args.tunnel_token_file), hermes_home=maybe(args.hermes_home),
        use_hermes=not args.no_hermes, install_hermes=args.install_hermes, no_push=args.no_push, fcm_credentials=maybe(args.fcm_credentials),
        apns_key=maybe(args.apns_key), apns_key_id=args.apns_key_id, apns_team_id=args.apns_team_id, apns_topic=args.apns_topic,
        wait_for_claim=not args.no_wait, yes=args.yes,
    ))


def _ops(args) -> int:
    from . import ops
    from .installer.run import Say
    from .installer.shell import Shell

    shell, say, paths = Shell(), Say(), ops.default_paths()
    if args.command == "doctor":
        return ops.doctor(shell, say, args.name, paths)
    if args.command == "public-url":
        return ops.public_url(say, paths, args.url)
    if args.command == "backup":
        try:
            target = ops.backup_database(Path(settings.db_path), Path(args.file) if args.file else None, paths.home)
        except (FileNotFoundError, FileExistsError, OSError) as error:
            print(error, file=sys.stderr)
            return 1
        print(f"Saved {target} ({target.stat().st_size // 1024} KiB). It holds your chats and paired-phone records: keep it private.")
        return 0
    if args.command == "service":
        if args.service_command == "status":
            return ops.service_status(shell, say, args.name, paths)
        if args.service_command == "restart":
            return ops.service_restart(shell, say, args.name, paths)
        return ops.service_logs(shell, args.name, paths, follow=args.follow, lines=args.lines)
    if args.push_command == "status":
        return ops.push_status(say, paths)
    if args.push_command == "android":
        return ops.push_android(shell, say, args.name, paths, Path(args.key) if args.key else None, restart=args.restart)
    if args.push_command == "ios":
        return ops.push_ios(shell, say, args.name, paths, Path(args.key) if args.key else None,
                            args.key_id, args.team_id, args.bundle_id, restart=args.restart)
    return ops.push_remove(shell, say, args.name, paths, args.platform, restart=args.restart)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command in (None, "serve"):
        from .main import run

        run()
        return 0
    if args.command == "install":
        return _install(args)
    if args.command == "uninstall":
        return _uninstall(args)
    if args.command == "pair":
        return asyncio.run(_with_database(lambda: _pair(args.ttl, args.url, not args.no_qr)))
    if args.command == "device":
        handlers = {"list": _device_list, "revoke": lambda: _device_revoke(args.device_id)}
        return asyncio.run(_with_database(handlers[args.device_command]))
    if args.command in ("doctor", "push", "service", "backup", "public-url"):
        return _ops(args)
    if args.command == "status":
        return asyncio.run(_with_database(_status))
    if args.command == "token":
        handlers = {
            "create": lambda: _token_create(args.name, args.scopes, args.expires_days),
            "list": _token_list,
            "revoke": lambda: _token_revoke(args.token_id),
        }
        return asyncio.run(_with_database(handlers[args.token_command]))
    if args.command == "hermes":
        return asyncio.run(_with_database(lambda: _hermes_connect(args.hermes_home, args.base_url, args.rotate)))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
