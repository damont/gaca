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

import base64
import hashlib
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

# The page look: the app's own "Slate" tokens as CSS variables (docs/chat-design.md §4), plus a
# dark set (data-dark, set by the SDK when the client reports a dark theme — the apps are
# light-only today), and a dense, flat default style for plain elements, so an unstyled page
# already looks right and the agent only writes CSS for what is unusual. Opt-in classes cover
# the shapes pages repeat (rows, tabs, stats, status words). Kept in step with
# skills/gaca-artifacts/references/sdk.md.
_THEME_CSS = """
:root{--bg:#F0F4F9;--surface:#FFFFFF;--text:#282F35;--muted:#5A6169;--faint:#747B83;--accent:#2D3C4C;
--on-accent:#F4F9FE;--border:#C8CED5;--rule:#D8DFE6;--chip:#D8DFE6;--danger:#B34D36;--ok:#3F7858;
--radius:4px;--font:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;color-scheme:light}
:root[data-dark]{--bg:#151C23;--surface:#1C252E;--text:#E6ECF2;--muted:#9AA6B2;--faint:#7F8B97;--accent:#A9C1D9;
--on-accent:#151C23;--border:#3A4856;--rule:#27323D;--chip:#27323D;--danger:#E07D63;--ok:#6FB08C;color-scheme:dark}
html{-webkit-text-size-adjust:100%}
body{margin:0;padding:12px 16px;background:var(--bg);color:var(--text);font:14px/1.4 var(--font);-webkit-tap-highlight-color:transparent}
h1{font-size:18px;font-weight:600;margin:0 0 8px}
h2{font-size:11px;font-weight:500;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin:18px 0 4px}
h3{font-size:14px;font-weight:600;margin:12px 0 4px}
p{margin:0 0 8px}
a{color:var(--accent);text-underline-offset:3px}
hr{border:0;border-top:1px solid var(--rule);margin:12px 0}
button,input,select,textarea{font:inherit;color:inherit}
button{background:none;border:0;padding:6px 2px;color:var(--accent);text-decoration:underline;text-underline-offset:3px;cursor:pointer}
button.primary{background:var(--accent);color:var(--on-accent);text-decoration:none;font-weight:600;padding:7px 16px;border-radius:var(--radius)}
input:not([type=checkbox]):not([type=radio]),select,textarea{display:block;width:100%;box-sizing:border-box;background:transparent;border:0;border-bottom:1px solid var(--border);border-radius:0;padding:6px 0;outline:0}
input:focus,select:focus,textarea:focus{border-bottom-color:var(--accent)}
input[type=checkbox],input[type=radio]{width:16px;height:16px;margin:0;accent-color:var(--accent);flex:none}
label.field{display:block;font-size:12px;color:var(--muted);margin:14px 0 2px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{font-size:11px;font-weight:500;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);text-align:left;padding:6px 6px 6px 0;border-bottom:1px solid var(--text)}
td{padding:7px 6px 7px 0;border-bottom:1px solid var(--rule);vertical-align:top}
.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.row{display:flex;align-items:center;gap:10px;padding:7px 0;border-bottom:1px solid var(--rule)}
.row.top{align-items:flex-start}
.grow{flex:1;min-width:0}
.sub{display:block;font-size:12.5px;color:var(--muted)}
.muted{color:var(--muted)}.faint{color:var(--faint)}.ok{color:var(--ok)}.bad{color:var(--danger)}
.done{color:var(--faint);text-decoration:line-through}
.tabs{display:flex;gap:16px;margin:10px 0 2px}
.tabs a,.tabs button{color:var(--muted);text-decoration:none;padding:0 0 3px;font-size:13px}
.tabs [aria-current]{color:var(--text);border-bottom:2px solid var(--text)}
.stats{display:flex}
.stat{flex:1;padding:0 12px;border-left:1px solid var(--rule)}
.stat:first-child{padding-left:0;border:0}
.stat b{display:block;font-size:22px;font-weight:600;line-height:1.1;font-variant-numeric:tabular-nums}
.stat span{font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
.kv{display:flex;justify-content:space-between;gap:12px;padding:6px 0;border-bottom:1px solid var(--rule);font-size:13px}
.kv>:first-child{color:var(--muted)}
.note{border-top:1px solid var(--border);border-bottom:1px solid var(--border);padding:8px 0;margin:14px 0;font-size:13px}
.seg{display:flex;border:1px solid var(--border);font-size:13px}
.seg button{flex:1;padding:5px 0;text-align:center;text-decoration:none;color:var(--muted)}
.seg [aria-pressed=true]{background:var(--text);color:var(--bg)}
.empty{color:var(--faint);padding:20px 0;text-align:center}
"""

# Same patterns as the Android port, kept as-is: `<head` also matches `<header>`, which only
# matters for a page with no <head> at all — and the CSP is also sent as a response header,
# so it holds even then.
_HEAD = re.compile(r"<head[^>]*>", re.IGNORECASE)
_HTML = re.compile(r"<html[^>]*>", re.IGNORECASE)


@cache
def sdk() -> str:
    return resources.files("gaca_api").joinpath("static/artifact-sdk.js").read_text(encoding="utf-8")


# ── the library shelf ────────────────────────────────────────────────────────
# Pages can't load anything from the network, so GACA ships a fixed set of pinned libraries and fonts
# (static/libs/manifest.json) that a page opts into with a marker tag:
#     <script data-gaca-lib="chart"></script>
# At build time the marker is replaced, in place, by the library's own code (or a @font-face rule with the
# font as a data: URI), so script order is the page's and the CSP stays network-free. The stored HTML keeps
# only the marker: it stays small, and the cloud's scanners see the page's own code, not the library's.
# Every file is checked against its sha256 in the manifest before it is used.
_LIB_TAG = re.compile(
    r"""<script\b[^>]*?\bdata-gaca-lib\s*=\s*["']?([A-Za-z0-9_-]{1,40})["']?[^>]*>\s*</script\s*>""", re.IGNORECASE
)


class LibraryError(RuntimeError):
    """A vendored library file is missing or doesn't match its pinned hash (a packaging bug, not the page's)."""


@cache
def libraries() -> dict[str, dict[str, Any]]:
    """The shelf's manifest: name -> {version, license, use, scripts?|font?, sha256, bytes}."""
    raw = resources.files("gaca_api").joinpath("static/libs/manifest.json").read_text(encoding="utf-8")
    return json.loads(raw)["libraries"]


def _lib_file(path: str, expected_sha256: str) -> bytes:
    data = resources.files("gaca_api").joinpath("static/libs", path).read_bytes()
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise LibraryError(f"library file {path} does not match its pinned hash")
    return data


@cache
def library_markup(name: str) -> str:
    """The inline markup that replaces one library's marker tag."""
    lib = libraries()[name]
    if font := lib.get("font"):
        b64 = base64.b64encode(_lib_file(font["file"], lib["sha256"][font["file"]])).decode("ascii")
        return (
            f"<style>@font-face{{font-family:'{font['family']}';font-style:normal;font-display:swap;"
            f"font-weight:{font['weight']};src:url(data:font/woff2;base64,{b64}) format('woff2')}}</style>"
        )
    parts = []
    for path in lib["scripts"]:
        code = _lib_file(path, lib["sha256"][path]).decode("utf-8")
        parts.append(f"<script>/* {name}: {path} */\n{code.replace('</script', '<\\/script')}\n</script>")
    if after := lib.get("after"):  # a one-line adapter for this page sandbox (e.g. no workers under the CSP)
        parts.append(f"<script>{after}</script>")
    return "".join(parts)


def inline_libraries(html: str) -> str:
    """Replace each `<script data-gaca-lib="NAME"></script>` with that library's code, once per library.
    An unknown name becomes a console error that lists what is available, so a page fails loudly, not silently."""
    seen: set[str] = set()

    def sub(m: re.Match[str]) -> str:
        name = m.group(1).lower()
        if name in seen:
            return ""
        seen.add(name)
        if name not in libraries():
            msg = f"GACA: unknown library {name!r}. Available: {', '.join(sorted(libraries()))}"
            return f"<script>console.error({json.dumps(msg)})</script>"
        return library_markup(name)

    return _LIB_TAG.sub(sub, html)


def build(html: str, artifact_id: str, title: str) -> str:
    """The agent's HTML with the prelude inserted right after <head> (or a <head> added after
    <html>, or a whole document around it if it's a bare fragment), and its library markers inlined.
    Libraries are inlined LAST, so a `<head` inside a library's code can't move where the prelude goes."""
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
        page = html[: head.end()] + prelude + html[head.end() :]
    elif root := _HTML.search(html):
        page = html[: root.end()] + "<head>" + prelude + "</head>" + html[root.end() :]
    else:
        page = f'<!doctype html><html><head><meta charset="utf-8">{prelude}</head><body>{html}</body></html>'
    return inline_libraries(page)


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
