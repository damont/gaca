"""Sharing and publishing a cloud page, as pass-throughs (Part 8b): members, invite links, the share and
publish previews, publishing, and the account's own templates.

The forwarder stays DUMB: each route below maps to exactly one cloud route, the body and the answer go
through unchanged, and the cloud decides roles, caps and checks. What this layer adds, and only this:

  * `user_confirmed: true` on the calls where the cloud wants it from a server token (creating an invite
    link, publishing, listing a template, deleting a template). That is honest because the routes are
    reachable only by a PAIRED DEVICE: the person's tap in the app is the confirmation. Hermes (an API
    token) cannot reach them, so no agent can set the flag for itself. Anything the app sends under that
    name is overwritten, not trusted.
  * the local pointer / list cache is kept in step (leaving removes the pointer; member changes refresh
    member_count on the next list).

The ROUTES (routers/cloud_gacabits.py) are an explicit allowlist with validated path ids. An invite link's
`url` (with its secret token) is returned to the app exactly once, as the cloud sends it, and is never
stored or logged here: nothing in this module keeps or prints a body.
"""
from __future__ import annotations

import json

from ..db import cloud as db
from . import cloud_client, cloud_link, cloud_list
from .exceptions import CloudError

_JSON = {"content-type": "application/json"}
NO_STORE = {"cache-control": "no-store"}


def _body(raw: bytes, *, confirm: bool) -> bytes | None:
    """The JSON object to forward: what the app sent, with `user_confirmed: true` set when the cloud needs it."""
    if not raw.strip():
        data: object = {}
    else:
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
    if not isinstance(data, dict):
        raise CloudError("The request body must be a JSON object.", status=422, code="request_invalid")
    if confirm:
        data["user_confirmed"] = True
    return json.dumps(data).encode()


async def forward(
    user_id: str, method: str, path: str, *, raw_body: bytes | None = None, confirm_body: bool = False,
    confirm_query: bool = False, no_store: bool = False, request_id: str | None = None,
) -> None:
    """Call the cloud route and hand its answer to the app unchanged (raises the CloudReply)."""
    content = _body(raw_body or b"", confirm=confirm_body) if raw_body is not None else None
    resp = await cloud_link.call(
        user_id, method, path, params={"user_confirmed": "true"} if confirm_query else None, content=content,
        headers=_JSON if content is not None else None, request_id=request_id)
    reply = cloud_link.reply_from(resp)
    if no_store:
        reply.headers.update(NO_STORE)
    if method != "GET":
        cloud_list.forget(user_id)  # members / templates changed: the next list asks the cloud again
    raise reply


async def leave(user_id: str, gid: str, request_id: str | None) -> None:
    resp = await cloud_link.call(user_id, "POST", f"/gacabits/{cloud_client.quote_id(gid)}/leave", request_id=request_id)
    await db.delete_pointer(user_id, gid)  # only reached when the cloud accepted the leave
    cloud_list.forget(user_id)
    raise cloud_link.reply_from(resp)
