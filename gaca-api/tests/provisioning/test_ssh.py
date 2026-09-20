"""Tests for the SSH provisioning layer, against a REAL local SSH server
(asyncssh's own, loopback-only) — no mocking of asyncssh internals. The
server actually execs whatever command is sent, on the test machine, so a
run of `echo ok` or `false` behaves exactly like it would against a real box.
"""
import asyncio
import os
from contextlib import asynccontextmanager

import asyncssh
import pytest

from gaca_api.models.user import SSHConnection
from gaca_api.provisioning.ssh import (
    SSHError,
    create_profile,
    discover_profiles,
    next_free_port,
    run,
)


class _AcceptingServer(asyncssh.SSHServer):
    """Accepts only the one client public key it was built with."""

    def __init__(self, authorized_key: asyncssh.SSHKey) -> None:
        self._authorized_key = authorized_key

    def begin_auth(self, username: str) -> bool:
        return True  # True = "yes, go ahead and authenticate"

    def public_key_auth_supported(self) -> bool:
        return True

    def validate_public_key(self, username: str, key: asyncssh.SSHKey) -> bool:
        return key == self._authorized_key


def _make_exec_command(home_override: str | None = None):
    """Builds a process handler that really runs whatever command the
    client asked for, on this machine — faithful behavior (real stdout,
    real exit code) without hand-simulating a shell. `home_override`, when
    given, runs the command with HOME pointed at a test-controlled
    directory, so a script that reads `Path.home()` (like the discovery
    script) sees a fake `~/.hermes/` laid out just for the test rather than
    whatever is actually in the test runner's real home directory.
    """

    async def exec_command(process: asyncssh.SSHServerProcess) -> None:
        env = {**os.environ, "HOME": home_override} if home_override else None
        proc = await asyncio.create_subprocess_shell(
            process.command or "true",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, _stderr = await proc.communicate()
        process.stdout.write(stdout.decode())
        process.exit(proc.returncode or 0)

    return exec_command


@asynccontextmanager
async def _local_ssh_server(*, home_override: str | None = None):
    """Spins up a loopback-only SSH server for the duration of the `with`
    block. Yields (port, client_private_key, wrong_private_key)."""
    server_key = asyncssh.generate_private_key("ssh-ed25519")
    client_key = asyncssh.generate_private_key("ssh-ed25519")
    wrong_key = asyncssh.generate_private_key("ssh-ed25519")

    server = await asyncssh.create_server(
        lambda: _AcceptingServer(client_key.convert_to_public()),
        host="127.0.0.1",
        port=0,
        server_host_keys=[server_key],
        process_factory=_make_exec_command(home_override),
    )
    try:
        yield server.sockets[0].getsockname()[1], client_key, wrong_key
    finally:
        server.close()
        await server.wait_closed()


def test_run_returns_stdout_on_success():
    async def go():
        async with _local_ssh_server() as (port, client_key, _wrong):
            conn = SSHConnection(
                host="127.0.0.1",
                port=port,
                username="david",
                private_key=client_key.export_private_key().decode(),
            )
            return await run(conn, "echo ok")

    assert asyncio.run(go()) == "ok\n"


def test_run_raises_on_non_zero_exit():
    async def go():
        async with _local_ssh_server() as (port, client_key, _wrong):
            conn = SSHConnection(
                host="127.0.0.1",
                port=port,
                username="david",
                private_key=client_key.export_private_key().decode(),
            )
            await run(conn, "exit 1")

    with pytest.raises(SSHError):
        asyncio.run(go())


def test_run_raises_on_unauthorized_key():
    async def go():
        async with _local_ssh_server() as (port, _client_key, wrong_key):
            conn = SSHConnection(
                host="127.0.0.1",
                port=port,
                username="david",
                private_key=wrong_key.export_private_key().decode(),
            )
            await run(conn, "echo ok")

    with pytest.raises(SSHError):
        asyncio.run(go())


def test_run_raises_when_nothing_is_listening():
    async def go():
        conn = SSHConnection(
            host="127.0.0.1",
            port=1,  # nothing listens on a privileged port in a test sandbox
            username="david",
            private_key=asyncssh.generate_private_key("ssh-ed25519")
            .export_private_key()
            .decode(),
        )
        await run(conn, "echo ok", timeout=2.0)

    with pytest.raises(SSHError):
        asyncio.run(go())


def _write_env(path, **values) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(f"{k}={v}" for k, v in values.items()) + "\n")


def test_discover_profiles_finds_default_and_named_profiles(tmp_path):
    """Mirrors the real layout confirmed on david-claw, 2026-09-20: the
    root .env is "default", each profiles/<name>/.env is a named one."""
    home = tmp_path / "home"
    _write_env(
        home / ".hermes" / ".env",
        API_SERVER_ENABLED="true", API_SERVER_HOST="0.0.0.0",
        API_SERVER_PORT="8642", API_SERVER_KEY="default-key",
    )
    _write_env(
        home / ".hermes" / "profiles" / "toolshed" / ".env",
        API_SERVER_ENABLED="true", API_SERVER_HOST="0.0.0.0",
        API_SERVER_PORT="8643", API_SERVER_KEY="toolshed-key",
    )
    _write_env(
        home / ".hermes" / "profiles" / "trading" / ".env",
        API_SERVER_ENABLED="true", API_SERVER_HOST="0.0.0.0",
        API_SERVER_PORT="8644", API_SERVER_KEY="trading-key",
    )

    async def go():
        async with _local_ssh_server(home_override=str(home)) as (port, client_key, _w):
            conn = SSHConnection(
                host="127.0.0.1", port=port, username="david",
                private_key=client_key.export_private_key().decode(),
            )
            return await discover_profiles(conn)

    found = asyncio.run(go())
    assert found == {
        "default": {"port": 8642, "api_key": "default-key"},
        "toolshed": {"port": 8643, "api_key": "toolshed-key"},
        "trading": {"port": 8644, "api_key": "trading-key"},
    }


def test_discover_profiles_skips_one_with_the_api_server_disabled(tmp_path):
    home = tmp_path / "home"
    _write_env(
        home / ".hermes" / "profiles" / "quiet" / ".env",
        API_SERVER_ENABLED="false", API_SERVER_PORT="9000", API_SERVER_KEY="k",
    )

    async def go():
        async with _local_ssh_server(home_override=str(home)) as (port, client_key, _w):
            conn = SSHConnection(
                host="127.0.0.1", port=port, username="david",
                private_key=client_key.export_private_key().decode(),
            )
            return await discover_profiles(conn)

    assert asyncio.run(go()) == {}


def test_discover_profiles_skips_one_missing_a_port_or_key(tmp_path):
    home = tmp_path / "home"
    _write_env(
        home / ".hermes" / "profiles" / "half-set-up" / ".env",
        API_SERVER_ENABLED="true", API_SERVER_PORT="9000",
        # no API_SERVER_KEY at all
    )

    async def go():
        async with _local_ssh_server(home_override=str(home)) as (port, client_key, _w):
            conn = SSHConnection(
                host="127.0.0.1", port=port, username="david",
                private_key=client_key.export_private_key().decode(),
            )
            return await discover_profiles(conn)

    assert asyncio.run(go()) == {}


def test_discover_profiles_handles_no_hermes_home_at_all(tmp_path):
    home = tmp_path / "home"
    home.mkdir()  # exists, but no .hermes under it

    async def go():
        async with _local_ssh_server(home_override=str(home)) as (port, client_key, _w):
            conn = SSHConnection(
                host="127.0.0.1", port=port, username="david",
                private_key=client_key.export_private_key().decode(),
            )
            return await discover_profiles(conn)

    assert asyncio.run(go()) == {}


# ── next_free_port ───────────────────────────────────────────────────

def test_next_free_port_is_one_past_the_highest_in_use(tmp_path):
    home = tmp_path / "home"
    _write_env(
        home / ".hermes" / ".env",
        API_SERVER_ENABLED="true", API_SERVER_PORT="8642", API_SERVER_KEY="k",
    )
    _write_env(
        home / ".hermes" / "profiles" / "trading" / ".env",
        API_SERVER_ENABLED="true", API_SERVER_PORT="8644", API_SERVER_KEY="k",
    )

    async def go():
        async with _local_ssh_server(home_override=str(home)) as (port, client_key, _w):
            conn = SSHConnection(
                host="127.0.0.1", port=port, username="david",
                private_key=client_key.export_private_key().decode(),
            )
            return await next_free_port(conn)

    assert asyncio.run(go()) == 8645  # past the highest (8644), not the count (2)


def test_next_free_port_falls_back_to_base_with_nothing_configured(tmp_path):
    home = tmp_path / "home"
    home.mkdir()

    async def go():
        async with _local_ssh_server(home_override=str(home)) as (port, client_key, _w):
            conn = SSHConnection(
                host="127.0.0.1", port=port, username="david",
                private_key=client_key.export_private_key().decode(),
            )
            return await next_free_port(conn)

    assert asyncio.run(go()) == 8642


# ── create_profile — against a FAKE hermes CLI, since the real one isn't
# available in a test sandbox. Faithful about what matters here: the exact
# argv sequence gaca-api sends and the .env it writes, not Hermes' own
# internals (already confirmed live against david-claw, 2026-09-20 — see
# the comment above _CREATE_PROFILE_SCRIPT in ssh.py).

_FAKE_HERMES = r"""#!/bin/bash
set -e
if [ "$1" = "profile" ] && [ "$2" = "create" ]; then
  dir="$HOME/.hermes/profiles/$3"
  if [ -d "$dir" ]; then
    echo "profile '$3' already exists" >&2
    exit 1
  fi
  mkdir -p "$dir"
  exit 0
elif [ "$1" = "--profile" ] && [ "$3" = "gateway" ] && [ "$4" = "install" ]; then
  touch "$HOME/.hermes/profiles/$2/.installed"
  exit 0
fi
echo "fake hermes: unrecognized args: $*" >&2
exit 1
"""


def _install_fake_hermes(home) -> None:
    """A `hermes` on PATH only in a login shell, matching the real box
    (hermes lives in ~/.local/bin, added to PATH by ~/.profile) — proves
    create_profile's `bash -lc` wrapping is actually doing something, not
    just cargo-culted."""
    bin_dir = home / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    hermes = bin_dir / "hermes"
    hermes.write_text(_FAKE_HERMES)
    hermes.chmod(0o755)
    (home / ".profile").write_text('export PATH="$HOME/bin:$PATH"\n')


def test_create_profile_runs_the_real_sequence_and_writes_the_env(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _install_fake_hermes(home)

    async def go():
        async with _local_ssh_server(home_override=str(home)) as (port, client_key, _w):
            conn = SSHConnection(
                host="127.0.0.1", port=port, username="david",
                private_key=client_key.export_private_key().decode(),
            )
            await create_profile(conn, "toolshed", 8643, "the-generated-key")

    asyncio.run(go())

    profile_dir = home / ".hermes" / "profiles" / "toolshed"
    assert (profile_dir / ".installed").is_file()  # gateway install really ran
    env_lines = (profile_dir / ".env").read_text().splitlines()
    assert "API_SERVER_ENABLED=true" in env_lines
    assert "API_SERVER_PORT=8643" in env_lines
    assert "API_SERVER_KEY=the-generated-key" in env_lines


def test_create_profile_preserves_unrelated_env_lines(tmp_path):
    """Upsert, not overwrite — a fresh profile's .env may have other content
    (Hermes seeds one from a template); confirm it survives untouched."""
    home = tmp_path / "home"
    home.mkdir()
    _install_fake_hermes(home)
    # Patch the fake CLI's own `profile create` step to seed an unrelated
    # line, the same way Hermes' real one seeds a .env from a template.
    hermes = home / "bin" / "hermes"
    hermes.write_text(_FAKE_HERMES.replace(
        'mkdir -p "$dir"\n  exit 0',
        'mkdir -p "$dir"\n  echo "SOME_OTHER_SETTING=keep-me" > "$dir/.env"\n  exit 0',
    ))
    hermes.chmod(0o755)

    async def go():
        async with _local_ssh_server(home_override=str(home)) as (port, client_key, _w):
            conn = SSHConnection(
                host="127.0.0.1", port=port, username="david",
                private_key=client_key.export_private_key().decode(),
            )
            await create_profile(conn, "toolshed", 8643, "k")

    asyncio.run(go())

    env_text = (home / ".hermes" / "profiles" / "toolshed" / ".env").read_text()
    assert "SOME_OTHER_SETTING=keep-me" in env_text
    assert "API_SERVER_PORT=8643" in env_text


def test_create_profile_raises_when_the_name_already_exists(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _install_fake_hermes(home)
    (home / ".hermes" / "profiles" / "toolshed").mkdir(parents=True)

    async def go():
        async with _local_ssh_server(home_override=str(home)) as (port, client_key, _w):
            conn = SSHConnection(
                host="127.0.0.1", port=port, username="david",
                private_key=client_key.export_private_key().decode(),
            )
            await create_profile(conn, "toolshed", 8643, "k")

    with pytest.raises(SSHError, match="already exists"):
        asyncio.run(go())
