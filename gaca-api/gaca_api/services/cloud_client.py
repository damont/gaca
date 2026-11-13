"""The one place this server talks to the GACA Cloud over the network.

Everything cloud-bound goes through `CloudClient`. It exists so that the rules about WHERE we
talk are in one spot and cannot be bypassed by a caller:

  * the base URL is pinned: it comes from the link record (copied from `settings.cloud_url`
    when linking started), never from a request, and must be https (unless the explicit dev
    flag `GACA_CLOUD_ALLOW_INSECURE_DEV` is on); no userinfo, no query, no fragment;
  * callers pass a PATH ("/gacabits/abc/data"), never a URL, and ids are percent-quoted, so a
    request cannot steer the call to another host or another route;
  * redirects are never followed (a redirect could carry the token elsewhere): a 3xx is an
    error, except 304 (an ETag hit, which is an answer);
  * TLS verification is on; there is no way to switch it off;
  * timeouts are explicit; a failure to connect, a timeout, an oversized answer all become
    `CloudUnavailable`, whose message never contains the URL or the token;
  * the token goes only in the Authorization header of the request and is never logged.

Tests use a fake cloud by setting `transport_override` to an `httpx.MockTransport` (or an
ASGI transport onto a stub app); production leaves it `None`.
"""
from __future__ import annotations

import logging
import uuid
from urllib.parse import quote, urlsplit

import httpx

from ..config import settings
from .exceptions import CloudError, CloudUnavailable

logger = logging.getLogger(__name__)



class _HideCloudAddress(logging.Filter):
    """httpx logs every request line (with the full URL) at INFO. The cloud's address is not a
    secret, but this server promises not to scribble it into logs: drop exactly those records
    (and nothing else the process logs through httpx)."""

    def __init__(self) -> None:
        super().__init__()
        self.needles: set[str] = set()

    def filter(self, record: logging.LogRecord) -> bool:
        text = record.getMessage()
        return not any(n in text for n in self.needles)


_hide = _HideCloudAddress()
for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).addFilter(_hide)

MAX_RESPONSE_BYTES = 16 * 1024 * 1024  # 12 MB is the biggest gacabit; this is a ceiling, not a rule
transport_override: httpx.AsyncBaseTransport | None = None

# What a caller may send on: the body's type and the cache validators. Nothing else is copied
# from the app's request (in particular never its Authorization header).
FORWARD_REQUEST_HEADERS = ("content-type", "accept", "if-match", "if-none-match")


def quote_id(value: str) -> str:
    """A path segment, safe to put in a URL: no slash, no dot-segment."""
    if value in ("", ".", ".."):
        raise CloudError("That id cannot be sent to the cloud.", status=422, code="bad_id")
    return quote(value, safe="")


def validate_base_url(url: str) -> str:
    """The pinned base URL, normalised (no trailing slash), or ValueError."""
    parts = urlsplit(url.strip())
    secure = parts.scheme == "https"
    if not (secure or (parts.scheme == "http" and settings.cloud_allow_insecure_dev)):
        raise ValueError("the cloud URL must be https")
    if not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("the cloud URL must be a plain address")
    return url.strip().rstrip("/")


class CloudClient:
    def __init__(self, base_url: str) -> None:
        try:
            self.base_url = validate_base_url(base_url)
        except ValueError as e:
            raise CloudError(f"Cloud linking is not set up correctly on this server ({e}).", status=500,
                             code="cloud_misconfigured") from None
        host = urlsplit(self.base_url).hostname or ""
        _hide.needles.add(host)

        def make(max_connections: int) -> httpx.AsyncClient:
            return httpx.AsyncClient(
                transport=transport_override,
                timeout=httpx.Timeout(settings.cloud_timeout_seconds, connect=settings.cloud_connect_timeout_seconds),
                follow_redirects=False,
                verify=True,
                trust_env=False,  # an environment proxy must not see the token
                limits=httpx.Limits(max_connections=max_connections, max_keepalive_connections=5),
            )

        # Long-polls (up to ~25 s each, one per open page) get their own pool so a screenful of
        # open pages cannot starve ordinary calls, writes and lists.
        self._http = make(20)
        self._poll = make(40)

    async def request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        params: dict | None = None,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
        request_id: str | None = None,
        timeout: float | httpx.Timeout | None = None,
        long_poll: bool = False,
    ) -> httpx.Response:
        if not path.startswith("/") or "://" in path or "?" in path or "#" in path:
            raise CloudError("Bad cloud path.", status=500, code="cloud_misconfigured")
        send = {k: v for k, v in (headers or {}).items() if k.lower() in FORWARD_REQUEST_HEADERS}
        send["X-Request-Id"] = request_id or uuid.uuid4().hex
        if token:
            send["Authorization"] = f"Bearer {token}"
        try:
            async with (self._poll if long_poll else self._http).stream(
                method, self.base_url + path, params=params, content=content, headers=send,
                timeout=timeout if timeout is not None else settings.cloud_timeout_seconds,
            ) as response:
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise CloudUnavailable("The cloud sent an answer that was too large.")
                # Rebuild a plain, fully-read response so callers can use .content/.json(). The bytes are
                # already decoded (gzip etc.), so the headers that describe the wire encoding must go,
                # or httpx would try to decode them a second time.
                kept = [(k, v) for k, v in response.headers.multi_items()
                        if k.lower() not in ("content-encoding", "content-length", "transfer-encoding")]
                return httpx.Response(response.status_code, headers=kept, content=bytes(body))
        except CloudUnavailable:
            raise
        except httpx.TimeoutException:
            logger.warning("cloud call timed out (%s)", method)
            raise CloudUnavailable("The cloud took too long to answer. Try again in a moment.") from None
        except httpx.HTTPError as e:
            # The exception text can hold the URL; log only its type.
            logger.warning("cloud call failed (%s: %s)", method, type(e).__name__)
            raise CloudUnavailable() from None

    async def aclose(self) -> None:
        await self._http.aclose()
        await self._poll.aclose()


_clients: dict[str, CloudClient] = {}


def client_for(base_url: str) -> CloudClient:
    """The shared client for this base URL (one connection pool per address)."""
    key = base_url.strip().rstrip("/")
    if key not in _clients:
        _clients[key] = CloudClient(key)
    return _clients[key]


async def aclose_all() -> None:
    clients = list(_clients.values())
    _clients.clear()
    for c in clients:
        await c.aclose()
