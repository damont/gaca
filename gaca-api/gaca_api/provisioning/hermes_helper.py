"""Running `hermes` for gaca-api when gaca-api itself is sandboxed.

Hermes assumes it may write anywhere in its home on every launch (install locks, leases, gateway
lock files, ...) and that changes from release to release, so a filesystem sandbox that lets gaca-api
spawn it directly keeps breaking. Instead the installer sets up a socket-activated systemd unit
(`<name>-hermes.socket` + `<name>-hermes@.service`, both managed by `gaca-api install | uninstall |
service | doctor`). systemd accepts each connection on `<data dir>/hermes.sock` and runs
`gaca-api hermes-helper` for it, as the same user but without the sandbox. There is no daemon to keep
alive: the helper exists only for the length of one request.

The protocol is one line of JSON each way:

    -> {"args": ["profile", "create", "scout", "--no-alias"], "timeout": 120}
    <- {"code": 0, "out": "...", "err": "..."}   or   {"error": "why it was refused"}

The helper runs only what `local.argv_allowed` permits (`--version`, `profile create <name>
--no-alias`, `profile delete <name> -y`): a compromised gaca-api can ask for nothing it could not
already ask for, and it no longer needs write access to anything of Hermes'.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

SOCKET_NAME = "hermes.sock"
MAX_REQUEST_BYTES = 4096
MAX_TIMEOUT = 170.0  # the longest the helper will let one command run; callers ask for less


class HelperError(Exception):
    """The helper could not be reached, or refused the request. The message is fit to show."""


def socket_path_for(data_dir: str | Path) -> Path:
    return Path(data_dir).expanduser() / SOCKET_NAME


# ── the client (gaca-api, doctor) ────────────────────────────────────

def _decode(raw: bytes) -> tuple[int, str, str]:
    try:
        reply = json.loads(raw.decode("utf-8", errors="replace"))
        if not isinstance(reply, dict):
            raise ValueError
    except ValueError:
        raise HelperError("the Hermes helper gave an answer gaca-api could not read") from None
    if "error" in reply:
        raise HelperError(f"the Hermes helper refused the request: {reply['error']}")
    try:
        return int(reply["code"]), str(reply.get("out", "")), str(reply.get("err", ""))
    except (KeyError, TypeError, ValueError):
        raise HelperError("the Hermes helper gave an answer gaca-api could not read") from None


def _unreachable(path: Path, error: OSError) -> HelperError:
    return HelperError(
        f"cannot reach the Hermes helper at {path} ({error.strerror or error}). It is installed with "
        "gaca-api: re-run ./install.sh, then check `gaca-api doctor`"
    )


async def call(path: str | Path, args: tuple[str, ...], timeout: float) -> tuple[int, str, str]:
    """Ask the helper to run one hermes command: (exit code, stdout, stderr)."""
    path = Path(path)
    request = json.dumps({"args": list(args), "timeout": timeout}) + "\n"
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(str(path)), 10)
    except (OSError, asyncio.TimeoutError) as error:
        raise _unreachable(path, error if isinstance(error, OSError) else OSError("timed out")) from None
    try:
        writer.write(request.encode())
        await writer.drain()
        writer.write_eof()
        raw = await asyncio.wait_for(reader.read(), timeout + 15)
    except asyncio.TimeoutError:
        raise HelperError(f"hermes {' '.join(args[:2])} timed out after {timeout:.0f}s") from None
    except OSError as error:
        raise _unreachable(path, error) from None
    finally:
        writer.close()
    return _decode(raw)


def call_sync(path: str | Path, args: tuple[str, ...], timeout: float) -> tuple[int, str, str]:
    return asyncio.run(call(path, args, timeout))


# ── the helper (what systemd runs per connection) ────────────────────

def handle(line: bytes, run) -> dict:
    """Turn one request line into a reply. `run(args, timeout)` -> (code, out, err) does the work;
    it is only reached for an allowlisted command."""
    from .local import argv_allowed

    try:
        request = json.loads(line.decode("utf-8"))
        args = request["args"]
        timeout = float(request.get("timeout", 120))
    except (ValueError, KeyError, TypeError, AttributeError):
        return {"error": "not a valid request"}
    if not (isinstance(args, list) and all(isinstance(a, str) for a in args)):
        return {"error": "not a valid request"}
    if not argv_allowed(tuple(args)):
        return {"error": "that hermes command is not on the allowlist"}
    try:
        code, out, err = run(tuple(args), min(max(timeout, 1.0), MAX_TIMEOUT))
    except Exception as error:  # LocalError (hermes missing, timed out) and anything unexpected
        return {"error": str(error)}
    return {"code": code, "out": out, "err": err}


def serve_stdio(stdin=None, stdout=None) -> int:
    """Answer the one request on stdin (the accepted socket) and exit."""
    from ..config import settings
    from .local import LocalHermes

    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout.buffer
    hermes = LocalHermes(settings.hermes_home, settings.hermes_bin)  # no socket: this one spawns

    def run(args, timeout):
        return asyncio.run(hermes._spawn(*args, timeout=timeout))

    line = stdin.readline(MAX_REQUEST_BYTES + 1)
    reply = {"error": "request too large"} if len(line) > MAX_REQUEST_BYTES else handle(line, run)
    stdout.write((json.dumps(reply) + "\n").encode())
    stdout.flush()
    return 0
