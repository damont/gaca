"""Local access to a Hermes install on the SAME machine as gaca-api.

This module is the whole of what gaca-api may do to the Hermes machine in
local mode, and it is deliberately small:

  READ    a fixed set of files under HERMES_HOME, never anything else:
          .env (only the API_SERVER_* lines are parsed, nothing else leaves
          this module), SOUL.md, profile.yaml, config.yaml (one flag), and
          hermes-agent/hermes_cli/default_soul.py (parsed, never executed).
  WRITE   only inside profiles/<name>/ (never `default`, never through a symlink):
            * .env, atomically:
                - on create: the profile's API_SERVER_* lines, plus GACA_BASE_URL / GACA_API_TOKEN
                  so its skills can reach GACA with no manual step;
                - afterwards: ONLY those two GACA_* keys, into a profile that has no GACA token yet;
            * skills/gaca/: GACA's own Hermes skills, copied from the repo's skills/ directory
              (provisioning/skill_sync.py: never overwrites a file an agent has edited).
  RUN     `hermes --version` and `hermes profile create <name> --no-alias`.
          Nothing else — `_hermes` refuses any other argv before spawning.

How the limits are kept (each one has a test in tests/provisioning/test_local.py):
  * profile names are matched against a strict pattern, so a name can only ever
    select a directory directly under profiles/, never a path;
  * files are opened with O_NOFOLLOW | O_NONBLOCK and must be regular files, so
    a symlink (or FIFO/device) planted in a profile cannot redirect a read;
    profile directories that are symlinks are refused outright;
  * reads are size-capped;
  * commands go through exec with an argv list — no shell anywhere — with
    stdin closed and a scrubbed environment, so gaca-api's own secrets (FCM
    path, database location) are never handed to a child process;
  * default_soul.py is evaluated by a whitelist AST walker, not imported.

This is an in-process boundary: it limits what *this code* does. The OS-level
layer (a hardened systemd unit — deploy/gaca-api.service) limits what the
process could do if the rest of the app were compromised. Be aware that gaca-api
also holds each crew member's Hermes API key and can start agent runs; a Hermes
agent can run commands, so this boundary is not what stands between a fully
compromised gaca-api and the box. See gaca-planning/hermes/0004-local-agent-mode.md.
"""
from __future__ import annotations

import ast
import asyncio
import os
import re
import shutil
import signal
import stat
import time
from pathlib import Path

import httpx

from . import skill_sync
from .base import ProvisioningError
from .envfile import has_gaca_token, upsert_env


class LocalError(ProvisioningError):
    """A local Hermes operation failed or was refused."""


# The only keys local mode may write into a profile besides its API_SERVER_* settings: how that
# profile's skills reach GACA. Nothing else about a profile's .env is ever touched.
GACA_ENV_KEYS = ("GACA_BASE_URL", "GACA_API_TOKEN")

PROFILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_API_KEY = re.compile(r"[A-Za-z0-9_-]{16,256}")
_KEY_VALUE = re.compile(r"[\x21-\x7e]+")  # printable ASCII, no whitespace
_API_SERVER_LINE = re.compile(r"^(API_SERVER_[A-Z_]+)=(.*)$")

_ENV_LIMIT = 64_000
_SOUL_LIMIT = 40_000
_YAML_LIMIT = 4_000
_SOURCE_LIMIT = 200_000
_OUTPUT_LIMIT = 2_000
DEFAULT_GATEWAY_PORT = 8642


# ── The only commands this module will ever run ──────────────────────

def argv_allowed(args: tuple[str, ...]) -> bool:
    """True only for the two hermes invocations local mode needs."""
    if args == ("--version",):
        return True
    return (
        len(args) == 4
        and args[0] == "profile"
        and args[1] == "create"
        and PROFILE_NAME.fullmatch(args[2]) is not None
        and args[2] != "default"
        and args[3] == "--no-alias"
    )


# ── Safe reading of default_soul.py ──────────────────────────────────

def _eval_text(node: ast.AST, names: dict, depth: int = 0):
    """Evaluate the tiny expression language default_soul.py is written in —
    string literals, names bound earlier in the file, `+`, tuples, and
    `.replace(str, str)` — and nothing else. Raises ValueError on anything
    outside that, so a hostile file cannot make us run code."""
    if depth > 40:
        raise ValueError("too deep")
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and node.id in names:
        return names[node.id]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _eval_text(node.left, names, depth + 1)
        right = _eval_text(node.right, names, depth + 1)
        if isinstance(left, str) and isinstance(right, str):
            return left + right
        raise ValueError("not text")
    if isinstance(node, ast.Tuple):
        return tuple(_eval_text(e, names, depth + 1) for e in node.elts)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "replace"
        and not node.keywords
        and len(node.args) == 2
    ):
        base = _eval_text(node.func.value, names, depth + 1)
        old = _eval_text(node.args[0], names, depth + 1)
        new = _eval_text(node.args[1], names, depth + 1)
        if all(isinstance(x, str) for x in (base, old, new)):
            return base.replace(old, new)
    raise ValueError("unsupported expression")


def stock_texts_from_source(source: str) -> list[str]:
    """Hermes' own "this persona carries no user intent" texts, read out of
    default_soul.py WITHOUT executing it. Empty on any surprise."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return []
    names: dict = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target, value = node.target, node.value
        else:
            continue
        if not isinstance(target, ast.Name):
            continue
        try:
            names[target.id] = _eval_text(value, names)
        except (ValueError, RecursionError):
            continue
    texts: list[str] = []
    default = names.get("DEFAULT_SOUL_MD")
    if isinstance(default, str):
        texts.append(default)
    legacy = names.get("_LEGACY_TEMPLATE_SOULS")
    if isinstance(legacy, tuple):
        texts.extend(t for t in legacy if isinstance(t, str))
    return texts


# ── File helpers ─────────────────────────────────────────────────────

def _read_regular_file(path: Path, limit: int) -> str | None:
    """Text of a regular, non-symlink file (at most `limit` bytes), else None.
    Never blocks on a FIFO and never follows a symlink on the final component."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        chunks, remaining = [], limit
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks).decode("utf-8", errors="replace")
    except OSError:
        return None
    finally:
        os.close(fd)


def _api_server_settings(env_text: str | None) -> dict | None:
    """`{"port", "api_key"}` if the .env enables the HTTP gateway, else None.
    Only API_SERVER_* lines are looked at; every other line is ignored."""
    if not env_text:
        return None
    values: dict[str, str] = {}
    for line in env_text.splitlines():
        match = _API_SERVER_LINE.match(line)
        if match:
            values[match.group(1)] = match.group(2).strip()
    if values.get("API_SERVER_ENABLED", "").lower() != "true":
        return None
    # Hermes serves on 8642 unless told otherwise, so an unset port is the default, not "disabled".
    port, key = values.get("API_SERVER_PORT") or str(DEFAULT_GATEWAY_PORT), values.get("API_SERVER_KEY", "")
    if not (port.isascii() and port.isdigit() and 0 < int(port) < 65536):
        return None
    if not _KEY_VALUE.fullmatch(key):
        return None
    return {"port": int(port), "api_key": key}


class LocalHermes:
    default_agent_host = "http://127.0.0.1"
    # Local mode can put a GACA token into a profile's .env (create_profile / write_gaca_env).
    can_wire_gaca = True

    def __init__(
        self,
        hermes_home: str | os.PathLike = "~/.hermes",
        hermes_bin: str = "hermes",
        skills_source: str | os.PathLike | None = None,
    ):
        self.home = Path(hermes_home).expanduser()
        self._skills_source = Path(skills_source).expanduser() if skills_source else skill_sync.default_source()
        self._bin_setting = hermes_bin
        self._bin: Path | None = None

    # -- paths ---------------------------------------------------------

    @property
    def _root(self) -> Path:
        return self.home.resolve()

    def _profile_dir(self, name: str) -> Path | None:
        """The directory for `name`, or None if there isn't a real one. Raises
        LocalError for a name that could not be a profile at all."""
        if name == "default":
            return self._root if self._root.is_dir() else None
        if not isinstance(name, str) or not PROFILE_NAME.fullmatch(name):
            raise LocalError(f"not a valid profile name: {name!r}")
        profiles = self._root / "profiles"
        if profiles.is_symlink() or not profiles.is_dir():
            return None
        directory = profiles / name
        if directory.is_symlink() or not directory.is_dir():
            return None
        return directory

    def _read_default_gateway_port(self) -> int:
        settings = _api_server_settings(_read_regular_file(self._root / ".env", _ENV_LIMIT))
        return settings["port"] if settings else DEFAULT_GATEWAY_PORT

    # -- discovery -----------------------------------------------------

    def _scan(self) -> dict[str, dict]:
        found: dict[str, dict] = {}
        default = _api_server_settings(_read_regular_file(self._root / ".env", _ENV_LIMIT))
        if default:
            found["default"] = default
        profiles = self._root / "profiles"
        if profiles.is_symlink() or not profiles.is_dir():
            return found
        for entry in sorted(profiles.iterdir()):
            if not PROFILE_NAME.fullmatch(entry.name) or entry.is_symlink() or not entry.is_dir():
                continue
            info = _api_server_settings(_read_regular_file(entry / ".env", _ENV_LIMIT))
            if info:
                found[entry.name] = info
        return found

    @staticmethod
    async def _served_by_default(default_port: int, name: str) -> bool:
        # Loopback only, port from a file we already parsed as 1..65535, name
        # already pattern-checked: nothing user-supplied reaches this URL.
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                response = await client.get(f"http://127.0.0.1:{default_port}/p/{name}/health")
            return response.status_code == 200
        except Exception:
            return False

    async def discover_profiles(self) -> dict[str, dict]:
        scanned = await asyncio.to_thread(self._scan)
        default = scanned.get("default")
        found: dict[str, dict] = {}
        for name, info in scanned.items():
            if name == "default":
                found[name] = {**info, "path": ""}
            elif default and await self._served_by_default(default["port"], name):
                found[name] = {"port": default["port"], "api_key": info["api_key"], "path": f"/p/{name}"}
            else:
                found[name] = {**info, "path": ""}
        return found

    async def next_free_port(self, *, base: int = DEFAULT_GATEWAY_PORT) -> int:
        found = await asyncio.to_thread(self._scan)
        return max((i["port"] for i in found.values()), default=base - 1) + 1

    # -- purpose files -------------------------------------------------

    def _read_profile_files(self, name: str) -> dict | None:
        directory = self._profile_dir(name)
        if directory is None:
            return None
        source = _read_regular_file(
            self._root / "hermes-agent" / "hermes_cli" / "default_soul.py", _SOURCE_LIMIT
        )
        return {
            "found": True,
            "soul": _read_regular_file(directory / "SOUL.md", _SOUL_LIMIT),
            "profile_yaml": _read_regular_file(directory / "profile.yaml", _YAML_LIMIT),
            "stock": stock_texts_from_source(source) if source else [],
        }

    async def read_profile_files(self, name: str) -> dict | None:
        if name != "default" and not (isinstance(name, str) and PROFILE_NAME.fullmatch(name)):
            raise LocalError(f"not a valid profile name: {name!r}")
        return await asyncio.to_thread(self._read_profile_files, name)

    # -- running hermes ------------------------------------------------

    def _resolve_bin(self) -> Path:
        if self._bin is not None:
            return self._bin
        setting = self._bin_setting
        if os.sep in setting:
            candidate: str | None = str(Path(setting).expanduser())
        else:
            search = os.pathsep.join(
                [os.environ.get("PATH", ""), str(Path.home() / ".local" / "bin")]
            )
            candidate = shutil.which(setting, path=search)
        if not candidate or not os.path.isfile(candidate) or not os.access(candidate, os.X_OK):
            raise LocalError(f"the hermes command was not found ({setting!r}); set GACA_HERMES_BIN")
        self._bin = Path(candidate)
        return self._bin

    def _child_env(self, exe: Path) -> dict[str, str]:
        # Deliberately NOT os.environ: gaca-api's own secrets must not reach a child.
        env = {
            "PATH": f"{exe.parent}:/usr/local/bin:/usr/bin:/bin",
            "HOME": str(Path.home()),
            "LANG": "C.UTF-8",
        }
        if self.home.resolve() != (Path.home() / ".hermes").resolve():
            env["HERMES_HOME"] = str(self._root)
        return env

    async def _hermes(self, *args: str, timeout: float) -> tuple[int, str, str]:
        if not argv_allowed(args):
            raise LocalError("refusing to run a hermes command that is not on the allowlist")
        exe = self._resolve_bin()
        try:
            proc = await asyncio.create_subprocess_exec(
                str(exe), *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._child_env(exe),
                cwd=str(self._root),
                start_new_session=True,
            )
        except OSError as error:
            raise LocalError(f"could not run hermes: {error}") from error
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.wait()
            raise LocalError(f"hermes {' '.join(args[:2])} timed out after {timeout:.0f}s") from None
        return (
            proc.returncode,
            out.decode("utf-8", errors="replace")[:_OUTPUT_LIMIT],
            err.decode("utf-8", errors="replace")[:_OUTPUT_LIMIT],
        )

    # -- creating a profile ---------------------------------------------

    def _write_profile_env(
        self, directory: Path, port: int, api_key: str, extra_env: dict[str, str] | None = None
    ) -> None:
        upsert_env(
            directory / ".env",
            {
                "API_SERVER_ENABLED": "true",
                "API_SERVER_HOST": "0.0.0.0",
                "API_SERVER_PORT": str(port),
                "API_SERVER_KEY": api_key,
                **(extra_env or {}),
            },
        )

    @staticmethod
    def _check_gaca_env(values: dict[str, str]) -> None:
        if not values or not set(values) <= set(GACA_ENV_KEYS):
            raise LocalError(f"only {', '.join(GACA_ENV_KEYS)} may be written into a profile")

    def _missing_gaca_token(self) -> list[str]:
        profiles = self._root / "profiles"
        if profiles.is_symlink() or not profiles.is_dir():
            return []
        return [
            entry.name
            for entry in sorted(profiles.iterdir())
            if PROFILE_NAME.fullmatch(entry.name)
            and not entry.is_symlink()
            and entry.is_dir()
            and not has_gaca_token(entry / ".env")
        ]

    async def profiles_missing_gaca_token(self) -> list[str]:
        """Named profiles (never `default`, whose .env is outside what gaca-api may write) that
        do not have a GACA API token yet."""
        return await asyncio.to_thread(self._missing_gaca_token)

    async def write_gaca_env(self, name: str, values: dict[str, str]) -> None:
        """Put GACA_BASE_URL / GACA_API_TOKEN into an existing profile's .env — those two keys
        only, and only for a real (non-symlinked) named profile."""
        if name == "default" or not (isinstance(name, str) and PROFILE_NAME.fullmatch(name)):
            raise LocalError(f"not a valid profile name: {name!r}")
        self._check_gaca_env(values)
        directory = self._profile_dir(name)
        if directory is None:
            raise LocalError(f"no profile called {name!r}")
        try:
            await asyncio.to_thread(upsert_env, directory / ".env", values)
        except ValueError as error:
            raise LocalError(str(error)) from error

    def _named_profiles(self) -> list[str]:
        profiles = self._root / "profiles"
        if profiles.is_symlink() or not profiles.is_dir():
            return []
        return [
            entry.name
            for entry in sorted(profiles.iterdir())
            if PROFILE_NAME.fullmatch(entry.name) and not entry.is_symlink() and entry.is_dir()
        ]

    async def named_profiles(self) -> list[str]:
        return await asyncio.to_thread(self._named_profiles)

    async def install_skills(self, name: str) -> None:
        """Copy GACA's Hermes skills into an existing named profile's `skills/gaca/` folder."""
        if name == "default" or not (isinstance(name, str) and PROFILE_NAME.fullmatch(name)):
            raise LocalError(f"not a valid profile name: {name!r}")
        directory = self._profile_dir(name)
        if directory is None:
            raise LocalError(f"no profile called {name!r}")
        try:
            await asyncio.to_thread(
                skill_sync.sync, self._skills_source, directory / "skills", create_skills_dir=True
            )
        except skill_sync.SkillSyncError as error:
            raise LocalError(f"installing skills into {name!r} failed: {error}") from error

    async def _expects_multiplex(self) -> bool:
        config = _read_regular_file(self._root / "config.yaml", _SOURCE_LIMIT)
        if config and re.search(r"^\s*multiplex_profiles:\s*true\b", config, re.M):
            return True
        try:
            code, out, _ = await self._hermes("--version", timeout=20)
        except LocalError:
            return False
        match = re.search(r"v?0\.(\d+)\.", out) if code == 0 else None
        return bool(match and int(match.group(1)) >= 21)

    async def create_profile(
        self, name: str, port: int, api_key: str, extra_env: dict[str, str] | None = None
    ) -> tuple[int, str]:
        """Create a new Hermes profile and make it reachable at the default gateway's
        `/p/<name>`, returning `(port, path)`. `port` is only meaningful for a Hermes
        that runs a gateway per profile, which local mode does not support. `extra_env` (only
        GACA_BASE_URL / GACA_API_TOKEN) is written in the same atomic write as the profile's
        own settings, so its skills can reach GACA from the first moment."""
        if name == "default" or not (isinstance(name, str) and PROFILE_NAME.fullmatch(name)):
            raise LocalError(f"not a valid new profile name: {name!r}")
        if not (isinstance(api_key, str) and _API_KEY.fullmatch(api_key)):
            raise LocalError("not a valid API key")
        if extra_env is not None:
            self._check_gaca_env(extra_env)
        try:
            async with asyncio.timeout(150):
                return await self._create_profile(name, api_key, extra_env)
        except TimeoutError:
            raise LocalError(f"provisioning {name!r} failed at serve: timed out") from None

    async def _create_profile(
        self, name: str, api_key: str, extra_env: dict[str, str] | None = None
    ) -> tuple[int, str]:
        default_port = await asyncio.to_thread(self._read_default_gateway_port)
        code, _, err = await self._hermes("profile", "create", name, "--no-alias", timeout=120)
        if code != 0:
            raise LocalError(f"provisioning {name!r} failed at create: {err.strip()}")
        directory = self._profile_dir(name)
        if directory is None:
            raise LocalError(f"provisioning {name!r} failed at create: profile directory not found")
        await asyncio.to_thread(self._write_profile_env, directory, default_port, api_key, extra_env)

        # The default gateway rescans profiles/ about every 30s; only a Hermes that does that
        # (>= 0.21, or multiplex_profiles on) can serve the new profile.
        others = [n for n in (await asyncio.to_thread(self._scan)) if n not in (name, "default")]
        multiplexed = False
        for other in others:
            if await self._served_by_default(default_port, other):
                multiplexed = True
                break
        if not multiplexed and not await self._expects_multiplex():
            raise LocalError(
                f"provisioning {name!r} failed at serve: this Hermes does not serve profiles "
                "from its default gateway (local mode needs Hermes 0.21 or newer)"
            )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if await self._served_by_default(default_port, name):
                return default_port, f"/p/{name}"
            await asyncio.sleep(2)
        raise LocalError(
            f"provisioning {name!r} failed at serve: the default gateway did not start "
            "serving the new profile within 60s"
        )
