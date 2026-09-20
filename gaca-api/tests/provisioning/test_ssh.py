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
from gaca_api.provisioning.ssh import SSHError, discover_profiles, run


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
