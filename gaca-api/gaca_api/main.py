"""gaca-api application entry point.

Wires the layers together: lifespan (DB client), a domain-error handler, the
health probe, and the session router.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .db import client
from .routers import sessions
from .services.exceptions import NotFound


@asynccontextmanager
async def lifespan(app: FastAPI):
    client.connect()  # startup: open the shared Mongo client
    yield
    await client.close()  # shutdown


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
