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
from .services import auth as auth_service
from .services import setup as setup_service


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gaca-api", description="GACA API server and owner tools")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="start the server (the default)")
    code = sub.add_parser("setup-code", help="print a one-time code to claim a new server from the app")
    code.add_argument("--ttl", type=int, default=setup_service.DEFAULT_TTL_MINUTES, metavar="MINUTES")
    code.add_argument("--url", help="the server's public https URL, to also print an app link")
    sub.add_parser("status", help="is this server claimed yet?")
    sub.add_parser("reset-password", help="set a new password for the owner")
    return parser


def _read_new_password() -> str:
    first = getpass.getpass("New password (min 8 characters): ")
    if len(first) < 8:
        raise SystemExit("Password must be at least 8 characters.")
    if first != getpass.getpass("Again: "):
        raise SystemExit("Passwords did not match.")
    return first


async def _setup_code(ttl: int, url: str | None) -> int:
    try:
        code, _ = await setup_service.issue_code(ttl)
    except setup_service.SetupRefused as error:
        print(error.detail, file=sys.stderr)
        return 1
    print(f"Setup code:  {code}   (single use, valid {ttl} minutes)")
    print('Open the app, enter this server\'s address, and choose "Set up this server".')
    if url:
        query = urlencode({"url": url.rstrip("/"), "code": code.replace("-", "")})
        print(f"Or open this link on your phone:  gaca://setup?{query}")
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


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command in (None, "serve"):
        from .main import run

        run()
        return 0
    if args.command == "setup-code":
        return asyncio.run(_with_database(lambda: _setup_code(args.ttl, args.url)))
    if args.command == "status":
        return asyncio.run(_with_database(_status))
    return asyncio.run(_with_database(_reset_password))


if __name__ == "__main__":
    raise SystemExit(main())
