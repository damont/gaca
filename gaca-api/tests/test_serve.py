"""The server listens on loopback TCP and a Unix socket at once (serve.py)."""
import asyncio
import os
import socket
import stat

import httpx
import pytest

from gaca_api import serve as serve_module


async def hello_app(scope, receive, send):
    if scope["type"] != "http":
        return
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": b"hello"})


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_bind_unix_socket_is_world_connectable(tmp_path):
    path = str(tmp_path / "api.sock")
    sock = serve_module.bind_unix_socket(path)
    try:
        assert stat.S_ISSOCK(os.stat(path).st_mode) and stat.S_IMODE(os.stat(path).st_mode) == 0o666
    finally:
        sock.close()


def test_bind_unix_socket_replaces_a_stale_socket_but_never_a_regular_file(tmp_path):
    path = str(tmp_path / "api.sock")
    serve_module.bind_unix_socket(path).close()  # leaves a stale socket file behind
    serve_module.bind_unix_socket(path).close()  # replaced, no EADDRINUSE
    precious = tmp_path / "data.db"
    precious.write_text("do not delete")
    with pytest.raises(FileExistsError):
        serve_module.bind_unix_socket(str(precious))
    assert precious.read_text() == "do not delete"


def test_serves_tcp_and_unix_socket_together_and_cleans_up(tmp_path):
    path, port = str(tmp_path / "api.sock"), free_port()

    async def go():
        task = asyncio.create_task(serve_module.serve(hello_app, "127.0.0.1", port, path))
        try:
            for _ in range(100):
                if os.path.exists(path):
                    break
                await asyncio.sleep(0.05)
            async with httpx.AsyncClient() as tcp_client:
                tcp = await tcp_client.get(f"http://127.0.0.1:{port}/")
            transport = httpx.AsyncHTTPTransport(uds=path)
            async with httpx.AsyncClient(transport=transport, base_url="http://x") as unix_client:
                unix = await unix_client.get("/")
            return tcp.text, unix.text
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    assert asyncio.run(go()) == ("hello", "hello")
    assert not os.path.exists(path)  # removed on shutdown


async def failing_startup_app(scope, receive, send):
    if scope["type"] == "lifespan":
        await receive()
        await send({"type": "lifespan.startup.failed", "message": "boom"})
        return


def test_an_app_that_fails_to_start_exits_nonzero_instead_of_hanging(tmp_path):
    for socket_path in (None, str(tmp_path / "api.sock")):
        with pytest.raises(SystemExit) as exit_info:
            asyncio.run(serve_module.serve(failing_startup_app, "127.0.0.1", free_port(), socket_path))
        assert exit_info.value.code == 3


def test_a_failed_startup_closes_the_database_so_the_process_can_exit(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from gaca_api import main
    from gaca_api.config import settings
    from gaca_api.db import client

    monkeypatch.setattr(settings, "db_path", str(tmp_path / "t.db"))
    monkeypatch.setattr(settings, "agent_engine", "")  # the real-world failure: engine unset
    with pytest.raises(RuntimeError, match="GACA_AGENT_ENGINE"):
        with TestClient(main.app):
            pass
    with pytest.raises(RuntimeError, match="not initialized"):
        client.connection()  # closed, not leaked
