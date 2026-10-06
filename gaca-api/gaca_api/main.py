"""gaca-api application entry point.

Wires the layers together: lifespan (DB client), a domain-error handler, the
health probe, and the session router.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .agent import runtime as agent_runtime
from .bus import event_bus
from .db import client
from .provisioning.ssh import SSHError
from .routers import agent, alerts, artifacts, auth, briefings, crew, lists, push, sessions
from .services import push as push_service
from .services.exceptions import (
    Conflict,
    CrewNotConfigured,
    InvalidRequest,
    InvalidTransition,
    NotFound,
    PayloadTooLarge,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await client.connect()  # startup: open SQLite + ensure the schema
    await event_bus.connect()  # bus for read_run -> open SSE streams
    agent_runtime.connect()  # resolve the configured engine module
    push_service.connect()  # Firebase Admin SDK, if GACA_FCM_CREDENTIALS_PATH is set
    yield
    push_service.close()  # shutdown, reverse order
    await agent_runtime.close()
    await event_bus.aclose()
    await client.close()


app = FastAPI(
    title="gaca-api",
    description="GACA API — used by the Android app, the web UI, and AI agents",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/api/agent",  # Swagger UI, under /api so nginx proxies it
    openapi_url="/api/openapi.json",  # machine-readable schema for agents
    redoc_url=None,  # one docs UI is enough
)


# Matches calendarapp and toolshed. In practice both dev and production serve
# the frontend and API from one origin — Vite proxies /api locally, nginx does
# it in production — so browsers never make a cross-origin request. This is
# here for direct API callers and to keep the app shape consistent.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(NotFound)
async def handle_not_found(request: Request, exc: NotFound) -> JSONResponse:
    """Map the service layer's NotFound to HTTP 404, in one place."""
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(InvalidTransition)
async def handle_invalid_transition(
    request: Request, exc: InvalidTransition
) -> JSONResponse:
    """Map an illegal state change to HTTP 409, in one place."""
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(InvalidRequest)
async def handle_invalid_request(
    request: Request, exc: InvalidRequest
) -> JSONResponse:
    """Map a semantically-invalid request body to HTTP 422, in one place."""
    return JSONResponse(status_code=422, content={"detail": str(exc)})


@app.exception_handler(Conflict)
async def handle_conflict(request: Request, exc: Conflict) -> JSONResponse:
    """Map a lost write race / duplicate create to HTTP 409, in one place."""
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(PayloadTooLarge)
async def handle_payload_too_large(request: Request, exc: PayloadTooLarge) -> JSONResponse:
    """Map an oversized upload to HTTP 413, in one place."""
    return JSONResponse(status_code=413, content={"detail": str(exc)})


@app.exception_handler(CrewNotConfigured)
async def handle_crew_not_configured(
    request: Request, exc: CrewNotConfigured
) -> JSONResponse:
    """Map a missing per-user crew config to HTTP 400, in one place."""
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(SSHError)
async def handle_ssh_error(request: Request, exc: SSHError) -> JSONResponse:
    """Map a failed provisioning call to HTTP 502 — gaca-api reached out to
    the user's own box on their behalf and that failed; not a client error,
    and not gaca-api's own fault either, hence 502 rather than 500."""
    return JSONResponse(status_code=502, content={"detail": str(exc)})


@app.get("/api/health")
def health() -> dict:
    """Liveness probe. Does no I/O so it can't fail for downstream reasons.

    Lives under /api like every other route: in production nginx proxies
    `location /api/` to this service without stripping the prefix, so a route
    outside /api would be unreachable through the front door.
    """
    return {"status": "ok", "service": app.title, "version": app.version}


@app.get("/api/schema")
async def get_schema():
    """Convenience endpoint returning the OpenAPI schema.

    Same document as /api/openapi.json, reachable with a plain GET — the agent
    setup guide points here so a runtime without a docs browser can still
    discover the API.
    """
    return app.openapi()


# Routers carry their resource prefix ("/auth", "/sessions"); the "/api" mount
# point is applied here, matching how calendarapp and toolshed mount theirs.
app.include_router(auth.router, prefix="/api")
app.include_router(agent.router, prefix="/api")
app.include_router(crew.router, prefix="/api")
app.include_router(alerts.router, prefix="/api")
app.include_router(push.router, prefix="/api")
app.include_router(sessions.router, prefix="/api")
app.include_router(briefings.router, prefix="/api")
app.include_router(lists.router, prefix="/api")
app.include_router(artifacts.router, prefix="/api")


def run() -> None:
    """Console-script entry point (`gaca-api`): start the ASGI server.

    Reads host/port from settings so deployment can override them via the
    environment (GACA_API_HOST / GACA_API_PORT). No --reload here — that's a
    dev-only convenience you still get from running uvicorn by hand.
    """
    import uvicorn

    from .config import settings

    uvicorn.run(app, host=settings.api_host, port=settings.api_port)
