"""An artifact as a page a client can show, and the calls that page makes back.

Both used to live in each client (the Android app's ArtifactDocument / ArtifactBridge),
which meant every client — Android, iPhone, web — re-implementing the same wrapping and the
same dispatch, and drifting. Here they're done once:

* `build` wraps the agent's HTML with a Content-Security-Policy that cuts the page off from
  the network, the app's colors as CSS variables, the base style, and the SDK
  (static/artifact-sdk.js) — all before the page's own markup, so its scripts find
  `window.app` ready and its CSS can still override the base style.
* `call` performs one `window.app.*` data call. The client's native bridge just forwards
  `{method, args}` to POST /artifacts/{id}/call and hands the answer back to the page.
  UI calls (ask, openUrl, toast) stay in the client — they're about its own screen.

The page is model-written and untrusted, so a call can only touch ITS OWN artifact's data,
args are whitelisted per method, and names/ids are checked before they reach the services.
"""
from __future__ import annotations

import json
import re
from functools import cache
from importlib import resources
from typing import Any

from pydantic import BaseModel, ValidationError

from ..schemas import artifact as s
from . import artifacts as svc
from .exceptions import BadRequest, InvalidRequest, RequestInvalid

# No connect-src / frame-src / form-action, so fetch, XHR, WebSocket, iframes and form posts
# all fall back to default-src 'none'. Images and fonts must be inline (data:).
CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "img-src data: blob:; font-src data:; media-src data: blob:; base-uri 'none'; form-action 'none'"
)

_THEME_CSS = "".join(
    [
        ":root{--bg:#F0F4F9;--surface:#FFFFFF;--text:#282F35;--muted:#747B83;--accent:#2D3C4C;",
        "--on-accent:#F4F9FE;--border:#C8CED5;--chip:#D8DFE6;--danger:#C55C43;--ok:#4C8C6A;",
        "--radius:12px;--font:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;color-scheme:light}",
        "html{-webkit-text-size-adjust:100%}",
        "body{margin:0;padding:16px;background:var(--bg);color:var(--text);font:16px/1.45 var(--font);",
        "-webkit-tap-highlight-color:transparent}",
        "button,input,select,textarea{font:inherit}",
    ]
)

# Same patterns as the Android port, kept as-is: `<head` also matches `<header>`, which only
# matters for a page with no <head> at all — and the CSP is also sent as a response header,
# so it holds even then.
_HEAD = re.compile(r"<head[^>]*>", re.IGNORECASE)
_HTML = re.compile(r"<html[^>]*>", re.IGNORECASE)


@cache
def sdk() -> str:
    return resources.files("gaca_api").joinpath("static/artifact-sdk.js").read_text(encoding="utf-8")


def build(html: str, artifact_id: str, title: str) -> str:
    """The agent's HTML with the prelude inserted right after <head> (or a <head> added after
    <html>, or a whole document around it if it's a bare fragment)."""
    init = json.dumps(
        {"artifact": {"id": artifact_id, "title": title}, "theme": {"dark": False, "accent": "#2D3C4C"}},
        separators=(",", ":"),
        ensure_ascii=False,
    ).replace("</", "<\\/")  # can't close the script tag early
    prelude = (
        f'<meta http-equiv="Content-Security-Policy" content="{CSP}">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<style>{_THEME_CSS}</style>"
        f"<script>window.__GACA_INIT={init};</script>"
        f"<script>{sdk().replace('</script', '<\\/script')}</script>"
    )
    if head := _HEAD.search(html):
        return html[: head.end()] + prelude + html[head.end() :]
    if root := _HTML.search(html):
        return html[: root.end()] + "<head>" + prelude + "</head>" + html[root.end() :]
    return f'<!doctype html><html><head><meta charset="utf-8">{prelude}</head><body>{html}</body></html>'


async def page(user_id: str, artifact_id: str) -> str:
    """The artifact's current HTML, wrapped and ready to load."""
    artifact = await svc.get(user_id, artifact_id)
    return build(await svc.get_html(user_id, artifact_id), artifact.id, artifact.title)


# ── calls from the page ──────────────────────────────────────────────────────
def _only(args: dict[str, Any], *keys: str) -> dict[str, Any]:
    """Just the named fields the page sent, nulls dropped — anything else is ignored."""
    return {k: args[k] for k in keys if args.get(k) is not None}


def _body[M: BaseModel](model: type[M], args: dict[str, Any], *keys: str) -> M:
    """Validate a call's args as the matching endpoint's body, failing the same way it would."""
    try:
        return model.model_validate(_only(args, *keys))
    except ValidationError as e:
        raise RequestInvalid(e) from None


def _name(args: dict[str, Any]) -> str:
    value = args.get("collection")
    if not isinstance(value, str) or not s._NAME.match(value):
        raise InvalidRequest("bad collection name")
    return value


def _doc_id(args: dict[str, Any]) -> str:
    value = args.get("id")
    if not isinstance(value, str) or not s._DOC_ID.match(value):
        raise InvalidRequest("bad document id")
    return value


async def call(user_id: str, artifact_id: str, method: str, args: dict[str, Any]) -> BaseModel | None:
    """One `window.app.*` data call: the same result (or error) as the matching endpoint.
    Calls that endpoint's service function directly — no HTTP round trip."""
    a, u, i = args, user_id, artifact_id
    match method:
        case "data":
            since = a.get("since_rev")
            if since is not None and (not isinstance(since, int) or isinstance(since, bool) or since < 0):
                raise InvalidRequest("since_rev must be a non-negative integer")
            return await svc.snapshot(u, i, since)
        case "state.get":
            return await svc.get_state(u, i)
        case "state.set":
            return await svc.put_state(u, i, _body(s.StatePut, a, "state", "expected_version"))
        case "state.patch":
            return await svc.patch_state(u, i, _body(s.StatePatch, a, "patch", "expected_version"))
        case "doc.list":
            return await svc.list_collection(u, i, _name(a))
        case "doc.add":
            return await svc.add_doc(u, i, _name(a), _body(s.DocCreate, a, "data", "id"))
        case "doc.set":
            return await svc.put_doc(u, i, _name(a), _doc_id(a), _body(s.DocPut, a, "data", "expected_version"))
        case "doc.patch":
            return await svc.patch_doc(u, i, _name(a), _doc_id(a), _body(s.DocPatch, a, "patch", "expected_version"))
        case "doc.remove":
            await svc.delete_doc(u, i, _name(a), _doc_id(a))
            return None
        case "doc.clear":
            await svc.clear_collection(u, i, _name(a))
            return None
        case "batch":
            return await svc.batch(u, i, _body(s.Batch, a, "ops"))
        case "emit":
            return await svc.add_event(u, i, _body(s.EventIn, a, "name", "payload"))
        case _:
            # Includes ask / openUrl / toast: those act on the client's own screen, so the
            # client handles them itself and never sends them here.
            raise BadRequest(f"unknown method {method}")
