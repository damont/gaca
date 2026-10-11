"""The server (serve.py): serves on loopback TCP, and fails loudly if the app cannot start."""
import asyncio
import socket

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


def test_serves_over_tcp(tmp_path):
    port = free_port()

    async def go():
        task = asyncio.create_task(serve_module.serve(hello_app, "127.0.0.1", port))
        try:
            async with httpx.AsyncClient() as client:
                for _ in range(100):
                    try:
                        return (await client.get(f"http://127.0.0.1:{port}/")).text
                    except httpx.ConnectError:
                        await asyncio.sleep(0.05)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    assert asyncio.run(go()) == "hello"


async def failing_startup_app(scope, receive, send):
    if scope["type"] == "lifespan":
        await receive()
        await send({"type": "lifespan.startup.failed", "message": "boom"})
        return


def test_an_app_that_fails_to_start_exits_nonzero_instead_of_hanging(tmp_path):
    with pytest.raises(SystemExit) as exit_info:
        asyncio.run(serve_module.serve(failing_startup_app, "127.0.0.1", free_port()))
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
