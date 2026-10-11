"""Start the ASGI server on loopback TCP.

The API is reached from the internet through a Cloudflare Tunnel that connects to it directly, and
by local callers (Hermes skills at http://127.0.0.1:41700), so it only ever listens on loopback.
"""
from __future__ import annotations

import asyncio

import uvicorn


async def serve(app, host: str, port: int) -> None:
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port))
    await server.serve()
    if not server.started:
        # Same contract as uvicorn.run(): an app that never started is a failure, so a
        # supervisor (systemd Restart=on-failure) sees it instead of a clean exit.
        raise SystemExit(3)


def run_app(app, host: str, port: int) -> None:
    asyncio.run(serve(app, host, port))
