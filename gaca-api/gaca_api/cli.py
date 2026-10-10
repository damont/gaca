"""The `gaca-api` command. With no arguments it starts the server (what the systemd unit runs).

Owner-only operations live here rather than behind HTTP because having a shell on the
server *is* the proof of ownership: issuing the first-run setup code, and recovering a
lost password.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from pathlib import Path
from urllib.parse import urlencode

from .config import settings
from .db import client
from .db import users as users_db
from .models.base import utcnow
from .provisioning import hermes_connect
from .services import auth as auth_service
from .services import setup as setup_service
from .services import tokens as token_service
from .services.exceptions import InvalidRequest, NotFound
from .utils import tokens as token_utils


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gaca-api", description="GACA API server and owner tools")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="start the server (the default)")
    code = sub.add_parser("setup-code", help="print a one-time code to claim a new server from the app")
    code.add_argument("--ttl", type=int, default=setup_service.DEFAULT_TTL_MINUTES, metavar="MINUTES")
    code.add_argument("--url", help="the server's public https URL, to also print an app link")
    code.add_argument("--qr", action="store_true", help="also print a QR code of the app link (needs --url)")
    sub.add_parser("status", help="is this server claimed yet?")

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
    install.add_argument("--no-hermes", action="store_true", help="do not look for Hermes on this machine")
    install.add_argument("--no-wait", action="store_true", help="do not wait for you to claim the server in the app")
    install.add_argument("--yes", action="store_true", help="never ask questions")
    sub.add_parser("reset-password", help="set a new password for the owner")

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


def _read_new_password() -> str:
    first = getpass.getpass("New password (min 8 characters): ")
    if len(first) < 8:
        raise SystemExit("Password must be at least 8 characters.")
    if first != getpass.getpass("Again: "):
        raise SystemExit("Passwords did not match.")
    return first


async def _setup_code(ttl: int, url: str | None, qr: bool = False) -> int:
    try:
        code, _ = await setup_service.issue_code(ttl)
    except setup_service.SetupRefused as error:
        print(error.detail, file=sys.stderr)
        return 1
    print(f"Setup code:  {code}   (single use, valid {ttl} minutes)")
    print('Open the app, enter this server\'s address, and choose "Set up this server".')
    if url:
        query = urlencode({"url": url.rstrip("/"), "code": code.replace("-", "")})
        link = f"gaca://setup?{query}"
        print(f"Or open this link on your phone:  {link}")
        if qr:
            import segno

            segno.make(link, error="m").terminal(compact=True)
    elif qr:
        print("(--qr needs --url, the server's public https address)", file=sys.stderr)
    return 0


async def _status() -> int:
    claimed = await setup_service.is_claimed()
    print("claimed: yes — sign in with the owner account" if claimed else "claimed: no — run `gaca-api setup-code`")
    return 0


async def _reset_password() -> int:
    if await users_db.count() == 0:
        print("This server has no owner yet; use `gaca-api setup-code`.", file=sys.stderr)
        return 1
    owner = await users_db.first()
    await auth_service.set_password(owner, _read_new_password())
    print(f"Password updated for {owner.email}.")
    return 0


async def _owner_or_none():
    owner = await users_db.first()
    if owner is None:
        print("This server has no owner yet; use `gaca-api setup-code`.", file=sys.stderr)
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


def _install(args) -> int:
    from .installer.run import Options, run

    def maybe(value):
        return Path(value).expanduser() if value else None

    return run(Options(
        repo_root=Path(args.repo_root).resolve(), service=args.service, name=args.name,
        data_dir=maybe(args.data_dir), port=args.port, hostname=args.hostname, tunnel=args.tunnel,
        tunnel_token_file=maybe(args.tunnel_token_file), hermes_home=maybe(args.hermes_home),
        use_hermes=not args.no_hermes, wait_for_claim=not args.no_wait, yes=args.yes,
    ))


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command in (None, "serve"):
        from .main import run

        run()
        return 0
    if args.command == "install":
        return _install(args)
    if args.command == "setup-code":
        return asyncio.run(_with_database(lambda: _setup_code(args.ttl, args.url, args.qr)))
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
    return asyncio.run(_with_database(_reset_password))


if __name__ == "__main__":
    raise SystemExit(main())
