"""Start the ASGI server on loopback TCP and, optionally, a Unix socket too.

TCP serves local callers (Hermes skills at http://127.0.0.1:41700). The Unix socket
(GACA_API_SOCKET) is how the frontend's nginx container reaches an API that runs on the host
as a plain process: the container mounts the socket's directory, so nothing about the API has
to be exposed on a network interface or opened in the firewall.
"""
from __future__ import annotations

import asyncio
import os
import socket
import stat

import uvicorn


def bind_unix_socket(path: str, mode: int = 0o666) -> socket.socket:
    """A bound Unix socket at `path`, replacing a stale socket left by a previous run.

    World-connectable on purpose: the nginx worker in the container is a different uid. The
    directory it lives in is what restricts who can reach it, and every request still needs a
    valid JWT. Refuses to replace anything that is not a socket."""
    try:
        if not stat.S_ISSOCK(os.lstat(path).st_mode):
            raise FileExistsError(f"{path} exists and is not a socket")
        os.unlink(path)
    except FileNotFoundError:
        pass
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(path)
        os.chmod(path, mode)
    except BaseException:
        sock.close()
        raise
    return sock


async def serve(app, host: str, port: int, socket_path: str | None = None) -> None:
    config = uvicorn.Config(app, host=host, port=port)
    server = uvicorn.Server(config)
    if not socket_path:
        await server.serve()
    else:
        sockets = [config.bind_socket(), bind_unix_socket(socket_path)]
        try:
            await server.serve(sockets=sockets)
        finally:
            try:
                os.unlink(socket_path)
            except OSError:
                pass
    if not server.started:
        # Same contract as uvicorn.run(): an app that never started is a failure, so a
        # supervisor (systemd Restart=on-failure) sees it instead of a clean exit.
        raise SystemExit(3)


def run_app(app, host: str, port: int, socket_path: str | None = None) -> None:
    asyncio.run(serve(app, host, port, socket_path))
