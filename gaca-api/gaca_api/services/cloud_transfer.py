"""The ONE place this server talks to anything other than the pinned cloud address: the presigned
S3 URLs the cloud hands out for a bundle upload (PUT) and a bundle download (GET).

Why it exists at all: a bundle can be 12 MB, more than the cloud's API gateway takes in one request,
so the cloud gives us a short-lived URL on its S3 bucket instead. That URL arrives in an API answer,
so it is DATA FROM THE NETWORK, and fetching an address a remote party names is the classic way to
turn a server into a proxy into somebody's private network (SSRF). Hence the rules, all enforced
before any connection is made and again by the client itself:

  * `https` only; the host must be an AWS S3 endpoint (`<bucket>.s3.<region>.amazonaws.com`, the
    older `<bucket>.s3-<region>.amazonaws.com`, or the path-style `s3.<region>.amazonaws.com`);
    nothing else, in particular no IP address, no `localhost`, no other cloud;
  * no userinfo (`https://x.amazonaws.com@evil.com/` names evil.com), no backslash, whitespace,
    control or non-ASCII character (the characters different URL parsers disagree about), port 443 only;
  * the parsed host is checked a second time on the URL object the HTTP client will actually use;
  * redirects are never followed (a 3xx is an error); TLS verification is on; environment proxies
    are ignored; nothing from the cloud's link (token, request ids) is sent; the headers sent on the
    PUT are the ones the presign names and only `content-type`, `content-encoding` and `x-amz-*`;
  * size and time are capped both ways, and errors/logs never contain the URL (its query is a
    signature that is valid for minutes).
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urlsplit

import httpx

from ..config import settings
from . import cloud_client
from .exceptions import CloudError

logger = logging.getLogger(__name__)

# Tests point this at a fake S3 (an httpx.MockTransport); production leaves it None.
transport_override: httpx.AsyncBaseTransport | None = None

# `bucket.s3.amazonaws.com`, `bucket.s3.eu-west-1.amazonaws.com`, `bucket.s3-eu-west-1.amazonaws.com`,
# `bucket.s3.dualstack.eu-west-1.amazonaws.com`, and path style `s3.eu-west-1.amazonaws.com`.
_HOST = re.compile(r"^(?:[a-z0-9](?:[a-z0-9.-]{0,61}[a-z0-9])?\.)?s3(?:[.-][a-z0-9]+(?:-[a-z0-9]+)*)*\.amazonaws\.com$")
_SAFE_URL = re.compile(r"^[\x21-\x7e]+$")  # printable ASCII, no space
_PUT_HEADERS = re.compile(r"^(content-type|content-encoding|x-amz-[a-z0-9-]+)$")

# The httpx request log line carries the whole URL; for these it would carry a signature.
cloud_client._hide.needles.update({"amazonaws.com", "X-Amz-"})  # noqa: SLF001


def _rejected(why: str) -> CloudError:
    return CloudError(f"The cloud gave a transfer address this server will not use ({why}).",
                      status=502, code="transfer_url_rejected", detail={"reason": why})


def check_url(url: object) -> str:
    """The URL if it is an https S3 address this server may talk to, else CloudError(502, transfer_url_rejected)."""
    if not isinstance(url, str) or not url or len(url) > 4096:
        raise _rejected("not an address")
    if not _SAFE_URL.match(url) or "\\" in url:
        raise _rejected("unusual characters")
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        port = parts.port
    except ValueError:
        raise _rejected("not an address") from None
    if parts.scheme != "https":
        raise _rejected("not https")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise _rejected("it carries a user name")
    if port not in (None, 443):
        raise _rejected("unexpected port")
    if not _HOST.match(host) or ".." in host:
        raise _rejected("not an S3 address")
    if parts.fragment:
        raise _rejected("unexpected fragment")
    # What the HTTP client will really connect to must be what we just checked.
    try:
        used = httpx.URL(url)
    except httpx.InvalidURL:
        raise _rejected("not an address") from None
    if used.scheme != "https" or used.host != host or used.userinfo:
        raise _rejected("ambiguous address")
    return url


def check_put_headers(headers: object) -> dict[str, str]:
    """The headers the presign says the PUT must carry, if they are the harmless kind."""
    if headers is None:
        return {}
    if not isinstance(headers, dict) or len(headers) > 16:
        raise _rejected("odd upload headers")
    out: dict[str, str] = {}
    for key, value in headers.items():
        if not isinstance(key, str) or not isinstance(value, str) or not _PUT_HEADERS.match(key.lower()) \
                or "\r" in value or "\n" in value or len(value) > 512:
            raise _rejected("odd upload headers")
        out[key] = value
    return out


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=transport_override, follow_redirects=False, verify=True, trust_env=False,
        timeout=httpx.Timeout(settings.cloud_transfer_timeout_seconds, connect=settings.cloud_connect_timeout_seconds),
    )


def _failed(what: str, status: int | None = None) -> CloudError:
    detail = {"transfer": what} | ({"status": status} if status is not None else {})
    return CloudError(f"Could not {what} the bundle with the cloud's storage. Nothing changed; try again.",
                      status=502, code="transfer_failed", detail=detail)


async def put(url: str, headers: dict[str, str], data: bytes) -> None:
    """PUT the bundle bytes to a presigned URL. No auth header, no redirects."""
    check_url(url)
    send = check_put_headers(headers)
    if len(data) > settings.cloud_max_transfer_bytes:
        raise CloudError("The bundle is larger than the cloud accepts.", status=413, code="too_large_for_cloud")
    try:
        async with _client() as client:
            resp = await client.put(url, content=data, headers=send)
    except httpx.HTTPError as e:
        logger.warning("bundle upload failed (%s)", type(e).__name__)  # the exception text can hold the URL
        raise _failed("upload") from None
    if resp.status_code not in (200, 201, 204):
        logger.warning("bundle upload refused (HTTP %s)", resp.status_code)
        raise _failed("upload", resp.status_code)


async def get(url: str) -> bytes:
    """GET a bundle from a presigned URL, at most `cloud_max_transfer_bytes` of it. The bytes are
    returned exactly as stored (gzip), whatever Content-Encoding header S3 puts on them."""
    check_url(url)
    cap = settings.cloud_max_transfer_bytes
    try:
        async with _client() as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    logger.warning("bundle download refused (HTTP %s)", resp.status_code)
                    raise _failed("download", resp.status_code)
                body = bytearray()
                try:
                    async for chunk in resp.aiter_raw():  # wire bytes: S3 labels the gzip file Content-Encoding: gzip
                        body += chunk
                        if len(body) > cap:
                            raise CloudError("The cloud's download is larger than this server accepts.",
                                             status=502, code="bundle_invalid", detail={"max_bytes": cap})
                except httpx.StreamConsumed:  # a test transport hands over a response that is already in memory
                    body = bytearray(resp.content[: cap + 1])
                    if len(body) > cap:
                        raise CloudError("The cloud's download is larger than this server accepts.",
                                         status=502, code="bundle_invalid", detail={"max_bytes": cap}) from None
    except httpx.HTTPError as e:
        logger.warning("bundle download failed (%s)", type(e).__name__)
        raise _failed("download") from None
    return bytes(body)
