"""gaca-api application entry point.

Wires the layers together: lifespan (DB client), a domain-error handler, the
health probe, and the session router.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .agent import runtime as agent_runtime
from .agent.base import EngineUnavailable
from .bus import event_bus
from .db import client
from .provisioning.base import ProvisioningError
from .config import settings
from .security import BodyLimitMiddleware, SecurityHeadersMiddleware, cors_origins
from .routers import agent, alerts, artifacts, auth, briefings, crew, devices, lists, push, sessions, setup, tokens
from .services import push as push_service
from .services.exceptions import (
    BadRequest,
    Conflict,
    CrewNotConfigured,
    InvalidRequest,
    InvalidTransition,
    NotFound,
    PayloadTooLarge,
    RequestInvalid,
)


logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await client.connect()  # startup: open SQLite + ensure the schema
    try:
        await event_bus.connect()  # bus for read_run -> open SSE streams
        agent_runtime.connect()  # resolve the configured engine module
        push_service.connect()  # Firebase Admin SDK / APNs client, whichever is configured
    except BaseException:
        # A failed startup must not leave the SQLite worker thread running: it is non-daemon,
        # so the process would hang instead of exiting and a supervisor would never restart it.
        with suppress(Exception):
            await event_bus.aclose()
        with suppress(Exception):
            await client.close()
        raise
    yield
    # Shutdown, reverse order. Every step is attempted even if an earlier one fails: not being
    # able to close Firebase must not skip closing the database.
    for step in (push_service.aclose, agent_runtime.close, event_bus.aclose, client.close):
        try:
            await step()
        except Exception:
            logger.exception("shutdown step %s failed", getattr(step, "__qualname__", step))


app = FastAPI(
    title="gaca-api",
    description="GACA API — used by the Android app, the web UI, and AI agents",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/api/docs",  # Swagger UI. Not /api/agent: that is the real "is an agent connected" route, which the docs page shadowed
    openapi_url="/api/openapi.json",  # machine-readable schema for agents
    redoc_url=None,  # one docs UI is enough
)


# CORS is off unless GACA_CORS_ORIGINS names origins: the phone app is not a browser, and there is
# no web frontend any more. No credentials are ever sent cross-origin (tokens travel in a header).
if cors_origins(settings.cors_origins):
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins(settings.cors_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
    )
app.add_middleware(BodyLimitMiddleware)
app.add_middleware(SecurityHeadersMiddleware)


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


@app.exception_handler(RequestInvalid)
async def handle_request_invalid(request: Request, exc: RequestInvalid) -> JSONResponse:
    """A body the service validated itself: answer with FastAPI's own 422, errors
    located under "body" as if FastAPI had validated it — callers can't tell."""
    errors = [{**e, "loc": ("body", *e["loc"])} for e in exc.errors]
    return await request_validation_exception_handler(request, RequestValidationError(errors))


@app.exception_handler(BadRequest)
async def handle_bad_request(request: Request, exc: BadRequest) -> JSONResponse:
    """Map a request for an operation that doesn't exist to HTTP 400, in one place."""
    return JSONResponse(status_code=400, content={"detail": str(exc)})


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


@app.exception_handler(ProvisioningError)
async def handle_provisioning_error(request: Request, exc: ProvisioningError) -> JSONResponse:
    """Map a failed provisioning call to HTTP 502 — gaca-api reached out to
    the user's own box on their behalf and that failed; not a client error,
    and not gaca-api's own fault either, hence 502 rather than 500."""
    return JSONResponse(status_code=502, content={"detail": str(exc)})


@app.exception_handler(EngineUnavailable)
async def handle_engine_unavailable(request: Request, exc: EngineUnavailable) -> JSONResponse:
    """A crew member's engine couldn't be reached or refused a read: 502 with a message
    the app can show, never a bare 500."""
    return JSONResponse(status_code=502, content={"detail": str(exc)})


@app.get("/api/health")
def health() -> dict:
    """Liveness probe. Does no I/O so it can't fail for downstream reasons.

    Lives under /api like every other route: the app builds every URL from a
    base ending in /api/, so a route outside it would be unreachable.
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
app.include_router(setup.router, prefix="/api")
app.include_router(auth.router, prefix="/api")
app.include_router(tokens.router, prefix="/api")
app.include_router(devices.router, prefix="/api")
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
    from .config import settings
    from .serve import run_app

    run_app(app, settings.api_host, settings.api_port)
