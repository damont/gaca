"""The forwarding layer: for an artifact id whose local record is a cloud pointer, answer every
/artifacts/{id}/... route the apps and Hermes use by calling the cloud's /gacabits/{id}/...
with this server's stored token.

It is a DUMB PASS-THROUGH by design (decided by David, contracts/README.md):
  * it authenticates the caller (the router did that), forwards, and maps errors; it enforces no
    gacabit rule of its own (roles, caps, schemas, sizes, billing) because the cloud decides them;
  * status, body and ETag come back exactly as the cloud sent them (an extra `detail` is added to
    an error body so FastAPI-style readers keep working; the metadata routes add `home`/`shared_by`
    and the in-app `link`, see cloud_list.adapt);
  * the cloud's 401 (it rejects OUR token) is never passed on as 401: see cloud_link.check.

How a request reaches here: routers/artifacts.py puts `forward_if_pointer` on the whole router; it
runs before the local handler and, when the id is a pointer, raises `CloudReply` carrying the
answer (main.py turns that into the response). Ids that are not pointers fall through to the
unchanged local code.

Route map (local -> cloud, same id):
  GET/PATCH ""            -> GET/PATCH /gacabits/{id}      (metadata)
  DELETE ""               -> POST /gacabits/{id}/leave      DELETE means LEAVE for a cloud gacabit:
                             a member stops having it (the pointer goes, the cloud's copy stays);
                             the owner gets the cloud's 409 owner_cannot_leave, so an app's "delete"
                             button can never erase a shared gacabit for everyone.
  GET html, versions      -> same paths;   POST restore, opened -> same paths
  GET data (since_rev, wait), state, batch, collections/..., events, events/handled -> same paths
  GET page                -> cloud html wrapped HERE with the local SDK/CSP (artifact_page.build)
  POST call               -> the SDK operations mapped to the data routes above
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from ..config import settings
from ..db import cloud as db
from ..schemas import artifact as s
from . import artifact_page, cloud_client, cloud_link, cloud_list
from .exceptions import BadRequest, CloudError, CloudReply, InvalidRequest, RequestInvalid

logger = logging.getLogger(__name__)

_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_JSON = {"content-type": "application/json"}

# Same hardening the local /html and /page routes send.
HTML_CSP = (
    "sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline'; "
    "style-src 'unsafe-inline'; img-src data: blob:; font-src data:; media-src data: blob:"
)


def clean_request_id(value: str | None) -> str:
    """The caller's X-Request-Id if it is a plain token, else a fresh one."""
    return value if value and _REQUEST_ID.match(value) else uuid.uuid4().hex


@dataclass
class Inbound:
    """What the router saw, without FastAPI in it."""
    method: str
    tail: str  # what follows /artifacts/{id}/ ("" for the artifact itself)
    query: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    request_id: str = ""


# (method, tail regex) -> (kind, cloud path suffix template, allowed query keys)
_NAME = s.NAME_PATTERN.strip("^$")
_DOC = s.DOC_ID_PATTERN.strip("^$")
_ROUTES: list[tuple[str, re.Pattern, str, str, tuple[str, ...]]] = []


def _r(method: str, tail: str, kind: str, suffix: str | None = None, query: tuple[str, ...] = ()) -> None:
    _ROUTES.append((method, re.compile(tail), kind, "/" + tail if suffix is None else suffix, query))


for _m in ("GET", "PATCH"):
    _r(_m, "", "meta", "")
_r("DELETE", "", "leave", "/leave")
_r("GET", "html", "plain", query=("version",))
_r("GET", "page", "page")
_r("POST", "call", "call")
_r("POST", "opened", "plain")
_r("GET", "versions", "plain")
_r("POST", "restore", "meta", "/restore")
_r("GET", "data", "plain", query=("since_rev", "wait"))
for _m in ("GET", "PUT", "PATCH"):
    _r(_m, "state", "plain")
_r("POST", "batch", "plain")
for _m in ("GET", "POST", "DELETE"):
    _ROUTES.append((_m, re.compile(rf"collections/({_NAME})"), "plain", "/collections/{0}", ()))
for _m in ("GET", "PUT", "PATCH", "DELETE"):
    _ROUTES.append((_m, re.compile(rf"collections/({_NAME})/({_DOC})"), "plain", "/collections/{0}/{1}", ()))
_r("GET", "events", "plain", query=("after", "unhandled", "limit"))
_r("POST", "events", "plain")
_r("POST", "events/handled", "plain")


def _match(method: str, tail: str):
    for m, rx, kind, suffix, query in _ROUTES:
        hit = rx.fullmatch(tail)
        if m == method and hit:
            parts = [cloud_client.quote_id(g) for g in hit.groups()]
            return kind, suffix.format(*parts) if parts else suffix, query
    return None


# ── page cache (html by version, so a page still opens when the cloud is down) ──
_PAGES: OrderedDict[tuple[str, str, int], str] = OrderedDict()
_LATEST: dict[tuple[str, str], int] = {}
_PAGES_MAX = 32


def forget_pages() -> None:
    _PAGES.clear()
    _LATEST.clear()


def _remember_page(user_id: str, gid: str, version: int, html: str) -> None:
    _PAGES[(user_id, gid, version)] = html
    _PAGES.move_to_end((user_id, gid, version))
    _LATEST[(user_id, gid)] = version
    while len(_PAGES) > _PAGES_MAX:
        (u, g, v), _ = _PAGES.popitem(last=False)
        if _LATEST.get((u, g)) == v:
            _LATEST.pop((u, g), None)


# ── entry point ──────────────────────────────────────────────────────────────
async def forward_if_pointer(user_id: str, artifact_id: str, inbound: Inbound) -> None:
    """If `artifact_id` is a cloud pointer and the route is one we forward, answer it (by raising
    CloudReply, which carries the answer); otherwise return and let the local code run."""
    pointer = await db.get_pointer(user_id, artifact_id)
    if pointer is None:
        return
    matched = _match(inbound.method, inbound.tail)
    if matched is None:
        return
    kind, suffix, allowed = matched
    try:
        match kind:
            case "plain":
                await _plain(user_id, pointer.id, suffix, allowed, inbound)
            case "meta":
                await _meta(user_id, pointer, suffix, inbound)
            case "leave":
                await _leave(user_id, pointer, inbound)
            case "page":
                await _page(user_id, pointer, inbound)
            case "call":
                await _call(user_id, pointer, inbound)
    except CloudReply as reply:
        reply.headers.setdefault("x-request-id", inbound.request_id)
        raise


def _path(gid: str, suffix: str = "") -> str:
    return f"/gacabits/{cloud_client.quote_id(gid)}{suffix}"


def _query(inbound: Inbound, allowed: tuple[str, ...]) -> tuple[dict[str, str], float | None]:
    """The allowed query keys, and, for a long-poll (`wait`), a read timeout longer than the wait."""
    params = {k: v for k, v in inbound.query.items() if k in allowed}
    timeout = None
    if "wait" in params:
        try:
            wait = int(params["wait"])
            if wait < 0:
                raise ValueError
        except ValueError:
            raise InvalidRequest("wait must be a non-negative number of seconds") from None
        wait = min(wait, settings.cloud_max_wait_seconds)
        params["wait"] = str(wait)
        timeout = max(settings.cloud_timeout_seconds, wait + 10.0)
    return params, timeout


async def _plain(user_id: str, gid: str, suffix: str, allowed: tuple[str, ...], inbound: Inbound) -> None:
    params, timeout = _query(inbound, allowed)
    resp = await cloud_link.call(
        user_id, inbound.method, _path(gid, suffix), params=params or None, content=inbound.body or None,
        headers=inbound.headers, request_id=inbound.request_id, timeout=timeout,
    )
    reply = cloud_link.reply_from(resp)
    if inbound.tail == "html":
        reply.headers.update({
            "content-security-policy": HTML_CSP, "x-content-type-options": "nosniff", "cache-control": "no-store",
        })
    raise reply


async def _refresh(user_id: str, pointer, g: Any) -> None:
    p = cloud_list.pointer_from(user_id, pointer.account_id, g, pointer.position)
    if p is not None and p.id == pointer.id:
        await db.upsert_pointer(p)


async def _drop_if_gone(user_id: str, gid: str, reply: CloudReply) -> None:
    if reply.status == 404:  # the cloud says we have no access (any more): the pointer is stale
        await db.delete_pointer(user_id, gid)
        cloud_list.forget(user_id)


async def _meta(user_id: str, pointer, suffix: str, inbound: Inbound) -> None:
    try:
        resp = await cloud_link.call(
            user_id, inbound.method, _path(pointer.id, suffix), content=inbound.body or None,
            headers=inbound.headers, request_id=inbound.request_id,
        )
    except CloudReply as reply:
        if inbound.method == "GET":
            await _drop_if_gone(user_id, pointer.id, reply)
        raise
    reply = cloud_link.reply_from(resp)
    g = cloud_link._json(resp)
    if isinstance(g, dict):
        await _refresh(user_id, pointer, g)
        reply.body = json.dumps(cloud_list.adapt(g, position=pointer.position)).encode()
        reply.headers["content-type"] = "application/json"
        reply.headers.pop("etag", None)  # the body was rewritten, so the cloud's validator no longer fits it
    raise reply


async def _leave(user_id: str, pointer, inbound: Inbound) -> None:
    resp = await cloud_link.call(user_id, "POST", _path(pointer.id, "/leave"), request_id=inbound.request_id)
    await db.delete_pointer(user_id, pointer.id)  # only reached when the cloud accepted the leave
    cloud_list.forget(user_id)
    raise cloud_link.reply_from(resp)


# ── the WebView contract: /page and /call ────────────────────────────────────
async def _page(user_id: str, pointer, inbound: Inbound) -> None:
    key = (user_id, pointer.id)
    try:
        resp = await cloud_link.call(user_id, "GET", _path(pointer.id), request_id=inbound.request_id)
        g = cloud_link._json(resp)
        if not isinstance(g, dict) or not isinstance(g.get("version"), int):
            raise CloudError("The cloud answered in a way this server did not understand.", code="cloud_unavailable")
        await _refresh(user_id, pointer, g)
        version, title = g["version"], str(g.get("title") or pointer.title)
        html = _PAGES.get((user_id, pointer.id, version))
        if html is None:
            page_resp = await cloud_link.call(user_id, "GET", _path(pointer.id, "/html"), request_id=inbound.request_id)
            html = page_resp.text
            _remember_page(user_id, pointer.id, version, html)
    except CloudReply as reply:
        await _drop_if_gone(user_id, pointer.id, reply)
        raise
    except CloudError:
        version = _LATEST.get(key)
        if version is None or (user_id, pointer.id, version) not in _PAGES:
            raise
        # The cloud is out of reach but we have this page from before: it opens, its data calls fail.
        html, title = _PAGES[(user_id, pointer.id, version)], pointer.title
    wrapped = artifact_page.build(html, pointer.id, title)
    raise CloudReply(200, wrapped.encode(), {
        "content-type": "text/html; charset=utf-8", "content-security-policy": artifact_page.CSP,
        "x-content-type-options": "nosniff", "cache-control": "no-store",
    })


def _arg_body(model, args: dict[str, Any], *keys: str) -> dict[str, Any]:
    """The named args, nulls dropped. Only the shape of the envelope is checked here (so a
    wrong-typed field fails the way the local endpoint's 422 would); the cloud judges the content."""
    picked = {k: args[k] for k in keys if args.get(k) is not None}
    try:
        model.model_validate(picked)
    except ValidationError as e:
        raise RequestInvalid(e) from None
    return picked


def map_call(method: str, a: dict[str, Any]) -> tuple[str, str, dict | None, dict | None]:
    """An SDK call as (HTTP method, cloud path suffix, query, body). Unknown methods are the same
    400 the local /call gives (`ask`, `openUrl`, `toast` belong to the client)."""
    match method:
        case "data":
            query = {}
            since = a.get("since_rev")
            if since is not None:
                if not isinstance(since, int) or isinstance(since, bool) or since < 0:
                    raise InvalidRequest("since_rev must be a non-negative integer")
                query["since_rev"] = str(since)
            wait = a.get("wait")
            if wait is not None:
                if not isinstance(wait, int) or isinstance(wait, bool) or wait < 0:
                    raise InvalidRequest("wait must be a non-negative integer")
                query["wait"] = str(wait)
            return "GET", "/data", query or None, None
        case "state.get":
            return "GET", "/state", None, None
        case "state.set":
            return "PUT", "/state", None, _arg_body(s.StatePut, a, "state", "expected_version")
        case "state.patch":
            return "PATCH", "/state", None, _arg_body(s.StatePatch, a, "patch", "expected_version")
        case "doc.list":
            return "GET", f"/collections/{cloud_client.quote_id(artifact_page._name(a))}", None, None
        case "doc.add":
            return ("POST", f"/collections/{cloud_client.quote_id(artifact_page._name(a))}", None,
                    _arg_body(s.DocCreate, a, "data", "id"))
        case "doc.set":
            return ("PUT", f"/collections/{cloud_client.quote_id(artifact_page._name(a))}/"
                    f"{cloud_client.quote_id(artifact_page._doc_id(a))}", None,
                    _arg_body(s.DocPut, a, "data", "expected_version"))
        case "doc.patch":
            return ("PATCH", f"/collections/{cloud_client.quote_id(artifact_page._name(a))}/"
                    f"{cloud_client.quote_id(artifact_page._doc_id(a))}", None,
                    _arg_body(s.DocPatch, a, "patch", "expected_version"))
        case "doc.remove":
            return ("DELETE", f"/collections/{cloud_client.quote_id(artifact_page._name(a))}/"
                    f"{cloud_client.quote_id(artifact_page._doc_id(a))}", None, None)
        case "doc.clear":
            return "DELETE", f"/collections/{cloud_client.quote_id(artifact_page._name(a))}", None, None
        case "batch":
            return "POST", "/batch", None, _arg_body(s.Batch, a, "ops")
        case "emit":
            return "POST", "/events", None, _arg_body(s.EventIn, a, "name", "payload")
        case _:
            raise BadRequest(f"unknown method {method}")


async def _call(user_id: str, pointer, inbound: Inbound) -> None:
    try:
        body = s.ArtifactCall.model_validate_json(inbound.body or b"{}")
    except ValidationError as e:
        raise RequestInvalid(e) from None
    http_method, suffix, query, payload = map_call(body.method, body.args)
    timeout = None
    if query and "wait" in query:
        wait = min(int(query["wait"]), settings.cloud_max_wait_seconds)
        query["wait"] = str(wait)
        timeout = max(settings.cloud_timeout_seconds, wait + 10.0)
    resp = await cloud_link.call(
        user_id, http_method, _path(pointer.id, suffix), params=query,
        content=None if payload is None else json.dumps(payload).encode(),
        headers=_JSON if payload is not None else None, request_id=inbound.request_id, timeout=timeout,
    )
    # The local /call answers 200 with the endpoint's JSON, or `null` where that endpoint sends 204.
    raise CloudReply(200, resp.content if resp.content.strip() else b"null", {"content-type": "application/json"})
