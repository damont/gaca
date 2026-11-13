"""The cloud routes the apps (and Hermes) need besides artifacts: invite links and the template
gallery. Pass-throughs: the cloud decides everything; this server adds the stored token, keeps the
local pointer list in step, and (for a template) does the one local thing, creating the artifact.

`user_confirmed`: Hermes calls this server with an API token (`gaca_pat_...`), the app with a
paired-device token (`gaca_dev_...`). Joining a gacabit adds someone else's page to the owner's
list, so when the caller is an API token the request must say `user_confirmed: true`, which Hermes
sets only after the person said yes; without it: 403 `user_confirmation_required` (the same code
the cloud uses for the same idea). The app IS the person tapping "Trust and add", so it is not asked.
"""
from __future__ import annotations

import json

from pydantic import ValidationError

from ..schemas import artifact as s
from . import artifacts as artifact_service
from . import cloud_client, cloud_link, cloud_list
from .exceptions import CloudError

_TEMPLATE_QUERY = ("q", "category", "tag", "sort", "mine", "limit", "cursor")


def _body(data: dict) -> tuple[bytes, dict[str, str]]:
    return json.dumps(data).encode(), {"content-type": "application/json"}


async def invite_preview(user_id: str, token: str, request_id: str) -> None:
    content, headers = _body({"token": token})
    resp = await cloud_link.call(user_id, "POST", "/invites/preview", content=content, headers=headers,
                                 request_id=request_id)
    raise cloud_link.reply_from(resp)


async def invite_redeem(user_id: str, token: str, *, by_agent: bool, user_confirmed: bool, request_id: str) -> None:
    if by_agent and not user_confirmed:
        raise CloudError(
            "Joining a shared gacabit needs the person's go-ahead: ask them, then send user_confirmed: true.",
            status=403, code="user_confirmation_required")
    content, headers = _body({"token": token})
    resp = await cloud_link.call(user_id, "POST", "/invites/redeem", content=content, headers=headers,
                                 request_id=request_id)
    reply = cloud_link.reply_from(resp)
    g = cloud_link._json(resp)
    if isinstance(g, dict):
        await cloud_list.remember(user_id, g)  # it shows in the list straight away
        cloud_list.forget(user_id)
        reply.body = json.dumps(cloud_list.adapt(g)).encode()
        reply.headers["content-type"] = "application/json"
    raise reply


async def templates(user_id: str, query: dict[str, str], request_id: str) -> None:
    params = {k: v for k, v in query.items() if k in _TEMPLATE_QUERY}
    resp = await cloud_link.call(user_id, "GET", "/templates", params=params or None, request_id=request_id)
    raise cloud_link.reply_from(resp)


async def template(user_id: str, template_id: str, request_id: str) -> None:
    resp = await cloud_link.call(user_id, "GET", f"/templates/{cloud_client.quote_id(template_id)}",
                                 request_id=request_id)
    raise cloud_link.reply_from(resp)


async def _bundle(user_id: str, template_id: str, request_id: str) -> dict:
    """The template bundle for a server. The cloud returns it inline in `bundle`; if it only sends
    a `download_url` we do NOT fetch that (it could point anywhere): we ask the cloud's own bundle
    route on the pinned address instead."""
    tid = cloud_client.quote_id(template_id)
    content, headers = _body({"target": "server_bundle"})
    resp = await cloud_link.call(user_id, "POST", f"/templates/{tid}/get", content=content, headers=headers,
                                 request_id=request_id)
    data = cloud_link._json(resp)
    bundle = data.get("bundle") if isinstance(data, dict) else None
    if not isinstance(bundle, dict):
        resp = await cloud_link.call(user_id, "GET", f"/templates/{tid}/bundle", request_id=request_id)
        data = cloud_link._json(resp)
        bundle = data.get("bundle") if isinstance(data, dict) else None
    if not isinstance(bundle, dict):
        raise CloudError("The cloud did not send a template bundle.", code="cloud_unavailable")
    return bundle


async def template_get(user_id: str, template_id: str, request_id: str) -> None:
    """`POST /templates/{id}/get` with target server_bundle: the bundle, as the cloud answers it."""
    content, headers = _body({"target": "server_bundle"})
    resp = await cloud_link.call(
        user_id, "POST", f"/templates/{cloud_client.quote_id(template_id)}/get", content=content, headers=headers,
        request_id=request_id)
    raise cloud_link.reply_from(resp)


def bundle_to_create(bundle: dict) -> s.ArtifactCreate:
    """A `kind=template` bundle (html + schemas, never data) as a local artifact to create."""
    meta = bundle.get("meta")
    forbidden = {"state", "collections", "events", "html_versions"} & set(bundle)
    if bundle.get("kind") != "template" or forbidden or not isinstance(meta, dict):
        raise CloudError("That is not a template bundle this server can install.", status=422, code="bundle_invalid")
    try:
        return s.ArtifactCreate(
            title=meta.get("title"), html=bundle.get("html"), description=meta.get("description"),
            data_notes=meta.get("data_notes"), schemas=bundle.get("schemas") or {},
        )
    except ValidationError:
        raise CloudError("That template does not fit this server's limits.", status=422, code="bundle_invalid") from None


async def template_install(user_id: str, template_id: str, request_id: str) -> s.ArtifactOut:
    """Get a template and create the artifact from it here, on this server. Free in the cloud
    (`server_bundle` counts a download, nothing else); the new artifact is an ordinary local one."""
    body = bundle_to_create(await _bundle(user_id, template_id, request_id))
    return await artifact_service.create(user_id, body)
