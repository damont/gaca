"""gaca-api application entry point.

Wires the layers together: lifespan (DB client), a domain-error handler, the
health probe, and the session router.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .agent import runtime as agent_runtime
from .bus import event_bus
from .db import client
from .routers import sessions
from .services.exceptions import NotFound


@asynccontextmanager
async def lifespan(app: FastAPI):
    await client.connect()  # startup: open SQLite + ensure the schema
    await event_bus.connect()  # bus for read_run -> open SSE streams
    agent_runtime.connect()  # build the configured agent backend
    yield
    await agent_runtime.close()  # shutdown, reverse order
    await event_bus.aclose()
    await client.close()


app = FastAPI(title="gaca-api", version="0.1.0", lifespan=lifespan)


@app.exception_handler(NotFound)
async def handle_not_found(request: Request, exc: NotFound) -> JSONResponse:
    """Map the service layer's NotFound to HTTP 404, in one place."""
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.get("/health")
def health() -> dict:
    """Liveness probe. Does no I/O so it can't fail for downstream reasons."""
    return {"status": "ok", "service": app.title, "version": app.version}


app.include_router(sessions.router)


def run() -> None:
    """Console-script entry point (`gaca-api`): start the ASGI server.

    Reads host/port from settings so deployment can override them via the
    environment (GACA_API_HOST / GACA_API_PORT). No --reload here — that's a
    dev-only convenience you still get from running uvicorn by hand.
    """
    import uvicorn

    from .config import settings

    uvicorn.run(app, host=settings.api_host, port=settings.api_port)
