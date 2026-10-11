"""Edge protections the API provides itself.

These used to come from an nginx in front (security headers, a body-size cap). With the
API reached directly through the Cloudflare Tunnel there is no nginx, so they live here,
along with rate-limiting the one endpoint a stranger can reach without credentials (pairing).
"""
from __future__ import annotations

import ipaddress
import time
from collections import deque
from collections.abc import Callable

from fastapi import HTTPException, Request, status

from .config import settings


# ── headers ──────────────────────────────────────────────────────────

_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"strict-origin-when-cross-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
]


class SecurityHeadersMiddleware:
    """Adds the standard hardening headers to every response, unless a route set its own."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                present = {name.lower() for name, _ in message.get("headers", [])}
                message = {**message, "headers": [*message.get("headers", []), *[h for h in _HEADERS if h[0] not in present]]}
            await send(message)

        await self.app(scope, receive, send_with_headers)


# ── request size ─────────────────────────────────────────────────────

class BodyLimitMiddleware:
    """Rejects request bodies over `max_bytes` with 413 — by Content-Length when it is declared,
    and by counting when it is streamed, so a missing or lying header does not get past.

    For a streamed body the app is shown a client disconnect once the limit is passed (so it can
    never act on a truncated body as if it were complete), and whatever it answers is replaced
    by the 413. (Raising from `receive` would not do: FastAPI turns that into a 400.)"""

    def __init__(self, app, max_bytes: Callable[[], int] | int | None = None):
        self.app = app
        self._max = max_bytes

    def _limit(self) -> int:
        if callable(self._max):
            return self._max()
        return self._max if self._max is not None else settings.max_body_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limit = self._limit()
        declared = dict(scope["headers"]).get(b"content-length")
        if declared and declared.isdigit() and int(declared) > limit:
            return await self._reject(send)

        seen = 0
        exceeded = False
        rejected = False

        async def counting_receive():
            nonlocal seen, exceeded
            if exceeded:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > limit:
                    exceeded = True
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message):
            nonlocal rejected
            if exceeded:
                if not rejected:
                    rejected = True
                    await self._reject(send)
                return  # drop whatever the app tried to say
            await send(message)

        await self.app(scope, counting_receive, guarded_send)
        if exceeded and not rejected:
            await self._reject(send)

    @staticmethod
    async def _reject(send) -> None:
        body = b'{"detail":"Request body is too large"}'
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()), *_HEADERS]})
        await send({"type": "http.response.body", "body": body})


# ── rate limiting ────────────────────────────────────────────────────

class RateLimiter:
    """Sliding-window limiter: at most `limit` hits per `window` seconds per key. In memory, so it
    resets on restart — enough to make guessing a pairing code impractical."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}

    def hit(self, key: str, limit: int, window: float) -> float | None:
        """Record an attempt. None if allowed, else seconds until it would be."""
        now = self._clock()
        hits = self._hits.setdefault(key, deque())
        while hits and now - hits[0] >= window:
            hits.popleft()
        if len(hits) >= limit:
            return window - (now - hits[0])
        hits.append(now)
        if len(self._hits) > 10_000:  # bound memory under a flood of distinct keys
            for k in [k for k, v in self._hits.items() if not v or now - v[-1] >= window]:
                del self._hits[k]
        return None

    def reset(self) -> None:
        self._hits.clear()


limiter = RateLimiter()


def client_ip(request: Request) -> str:
    """Who is really calling. Behind the Cloudflare Tunnel every request arrives from localhost
    (cloudflared), with the real address in CF-Connecting-IP — trusted only when the immediate
    peer is loopback, so a direct caller cannot choose its own identity."""
    peer = request.client.host if request.client else "unknown"
    try:
        is_loopback = ipaddress.ip_address(peer).is_loopback
    except ValueError:
        is_loopback = False
    if is_loopback:
        forwarded = request.headers.get("cf-connecting-ip", "").strip()
        try:
            return str(ipaddress.ip_address(forwarded))
        except ValueError:
            pass
    return peer


def rate_limit(name: str, per_minute: Callable[[], int] | None = None):
    """A FastAPI dependency: 429 once a client exceeds `per_minute` calls to this endpoint group."""

    async def dependency(request: Request) -> None:
        limit = per_minute() if per_minute else settings.auth_attempts_per_minute
        wait = limiter.hit(f"{name}:{client_ip(request)}", limit, 60.0)
        if wait is not None:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Too many attempts. Try again in {int(wait) + 1} seconds.",
                headers={"Retry-After": str(int(wait) + 1)},
            )

    return dependency


def cors_origins(raw: str) -> list[str]:
    return [o.strip().rstrip("/") for o in raw.split(",") if o.strip()]
