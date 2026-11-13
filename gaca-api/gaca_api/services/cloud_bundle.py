"""Bundles: the neutral snapshot of a gacabit that crosses between this server and the cloud
(docs/cloud-link.md; the cloud's contract is bundle-format.md + bundle.schema.json, copied to
schemas/bundle.schema.json).

No network and no database here, only the data transformations:

  local snapshot ──build_bundle──▶ bundle ──pack──▶ gzip bytes          (move to the cloud)
  gzip bytes ──unpack──▶ bundle ──validate──▶ ──bundle_to_snapshot──▶ local snapshot   (bring home)

A bundle from the cloud is hostile input as far as this server is concerned: it is size-capped while
inflating (a 20 KB file that expands to 20 GB is refused at the cap), parsed without NaN/Infinity,
checked against the bundle schema, and every limit local artifacts live under is re-checked
before a single local row is written.
"""
from __future__ import annotations

import gzip
import json
import re
import zlib
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from ..config import settings
from ..models.artifact import ArtifactDoc
from ..schemas import artifact as s

BUNDLE_VERSION = 1
# What the cloud keeps (bundle-format.md): the 5 most recent page versions (the current one + 4 older),
# the 200 most recent events, and 25,000 documents over all collections.
CLOUD_VERSIONS_KEPT = 5
CLOUD_MAX_EVENTS = 200
CLOUD_MAX_DOCS = 25_000
CLOUD_MAX_SCHEMAS = 50
_ID = re.compile(r"[0-9a-f]{32}")

_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schemas" / "bundle.schema.json"


class BundleInvalid(ValueError):
    """The bundle (or the thing we were about to make one from) cannot be used. The message is safe to show."""

    def __init__(self, message: str, detail: dict | None = None) -> None:
        super().__init__(message)
        self.detail = detail or {}


def portable_id(artifact_id: str) -> bool:
    """The cloud keeps an id only when it is 32 lowercase hex characters (what this server mints)."""
    return bool(_ID.fullmatch(artifact_id))


def _size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode())


# ── local -> bundle ──────────────────────────────────────────────────────────
def build_bundle(snapshot: dict[str, Any], now: datetime) -> dict[str, Any]:
    """The `kind: full` bundle of a local artifact: everything the cloud keeps, nothing it does not
    (user, chat/alert links, position, last_opened_at, rev and per-document versions are local)."""
    a = snapshot["artifact"]
    doc = a["doc"]
    meta: dict[str, Any] = {
        "title": doc["title"], "archived": bool(a["archived"]), "created_at": doc["created_at"], "origin_id": a["id"],
    }
    for key in ("description", "data_notes", "crew"):
        if doc.get(key) is not None:
            meta[key] = doc[key]
    older = [v for v in snapshot["versions"] if v["version"] != a["version"]]
    older = sorted(older, key=lambda v: v["version"])[-(CLOUD_VERSIONS_KEPT - 1):]
    collections: dict[str, dict[str, Any]] = {}
    for d in sorted(snapshot["docs"], key=lambda d: (d["created_at"], d["doc_id"])):
        collections.setdefault(d["collection"], {})[d["doc_id"]] = {
            "data": d["data"], "created_at": d["created_at"], "updated_at": d["updated_at"]}
    bundle: dict[str, Any] = {
        "bundle_version": BUNDLE_VERSION, "kind": "full", "exported_at": now.isoformat(), "meta": meta,
        "html": a["html"], "html_version": a["version"],
        "schemas": doc.get("schemas") or {}, "state": a["state"], "state_version": a["state_version"],
        "collections": collections,
        "events": [{"name": e["name"], "created_at": e["created_at"], "payload": e["payload"]}
                   for e in snapshot["events"] if not e["handled"]],
    }
    if older:
        bundle["html_versions"] = [
            {"version": v["version"], "created_at": v["created_at"], "note": v["note"], "html": v["html"]}
            for v in older]
    return bundle


def too_big(snapshot: dict[str, Any], bundle: dict[str, Any]) -> list[dict[str, Any]]:
    """What stops this artifact fitting in the cloud, or [] when nothing does. Each entry names the
    thing, its size or count, and the limit, so the person knows what to trim."""
    found: list[dict[str, Any]] = []

    def over(what: str, size: int, limit: int, unit: str = "bytes") -> None:
        if size > limit:
            found.append({"item": what, "size": size, "limit": limit, "unit": unit})

    over("page (html)", len(bundle["html"].encode()), s.MAX_HTML_BYTES)
    for v in bundle.get("html_versions", []):
        over(f"page version {v['version']}", len(v["html"].encode()), s.MAX_HTML_BYTES)
    over("state", _size(bundle["state"]), s.MAX_STATE_BYTES)
    total_docs = 0
    for name, docs in bundle["collections"].items():
        over(f"collection {name!r}", len(docs), s.MAX_DOCS_PER_COLLECTION, "documents")
        total_docs += len(docs)
        for doc_id, d in docs.items():
            over(f"document {name}/{doc_id}", _size(d["data"]), s.MAX_DOC_BYTES)
    over("documents in all collections", total_docs, CLOUD_MAX_DOCS, "documents")
    over("unhandled events (mark some handled)", len(bundle["events"]), CLOUD_MAX_EVENTS, "events")
    over("collection schemas", len(bundle["schemas"]), CLOUD_MAX_SCHEMAS, "schemas")
    for i, e in enumerate(bundle["events"], 1):
        over(f"event {i}", _size(e["payload"]), s.MAX_DOC_BYTES)
    total = _size(bundle)
    if total > settings.cloud_max_bundle_bytes:
        parts = {"page (html) and versions": len(bundle["html"].encode()) + sum(
            len(v["html"].encode()) for v in bundle.get("html_versions", [])), "state": _size(bundle["state"])}
        for name, docs in bundle["collections"].items():
            parts[f"collection {name!r}"] = _size(docs)
        biggest = sorted(parts.items(), key=lambda kv: kv[1], reverse=True)[:5]
        found.append({"item": "the whole gacabit", "size": total, "limit": settings.cloud_max_bundle_bytes,
                      "unit": "bytes", "biggest": [{"item": k, "size": v} for k, v in biggest]})
    return found


def validate_bundle(bundle: Any) -> list[dict[str, str]]:
    """Schema errors of a bundle (at most 10), [] when it conforms to bundle.schema.json."""
    errors = sorted(_validator().iter_errors(bundle), key=lambda e: list(map(str, e.absolute_path)))
    return [{"path": "/" + "/".join(map(str, e.absolute_path)), "message": e.message[:200]} for e in errors[:10]]


@lru_cache(maxsize=1)
def _validator() -> Draft202012Validator:
    return Draft202012Validator(json.loads(_SCHEMA_PATH.read_text()))


def bad_numbers(bundle: dict[str, Any], limit: int = 10) -> list[str]:
    """Paths of numbers the cloud would refuse: integers outside int64, NaN and Infinity. Iterative (no
    recursion limit), CPU-bound: call it in a worker thread."""
    found: list[str] = []
    stack: list[tuple[Any, tuple]] = [(bundle, ())]
    while stack and len(found) < limit:
        value, path = stack.pop()
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            if not -(2 ** 63) <= value < 2 ** 63:
                found.append("/" + "/".join(map(str, path)))
        elif isinstance(value, float):
            if value != value or value in (float("inf"), float("-inf")):
                found.append("/" + "/".join(map(str, path)))
        elif isinstance(value, dict):
            stack.extend((v, path + (k,)) for k, v in value.items())
        elif isinstance(value, list):
            stack.extend((v, path + (i,)) for i, v in enumerate(value))
    return sorted(found)


def not_carried(snapshot: dict[str, Any]) -> dict[str, Any]:
    """What a move to the cloud does NOT keep (the cloud has no place for it, or keeps less). The person is
    told before it happens; the sealed backup still holds all of it for 30 days."""
    a = snapshot["artifact"]
    doc = a["doc"]
    older = [v for v in snapshot["versions"] if v["version"] != a["version"]]
    dropped = max(0, len(older) - (CLOUD_VERSIONS_KEPT - 1))
    links = {k: doc[k] for k in ("session_id", "alert_id") if doc.get(k)}
    out = {
        "versions_dropped": dropped,
        "handled_events_dropped": sum(1 for e in snapshot["events"] if e["handled"]),
        "crew": doc.get("crew") or None,
        "chat_alert_links": links or None,
        "doc_versions_reset": sum(1 for d in snapshot["docs"] if d["version"] > 1),
        "last_opened": bool(doc.get("last_opened_at")),
    }
    out["anything"] = bool(out["versions_dropped"] or out["handled_events_dropped"] or out["crew"]
                           or out["chat_alert_links"] or out["doc_versions_reset"] or out["last_opened"])
    return out


def canon(value: Any) -> str:
    """Canonical JSON: sorted keys, no NaN, no ASCII escaping. Unlike Python `==` it tells True from 1 and
    1.0 from 1, and two spellings of the same text (NFC/NFD) stay different."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _instant(value: Any) -> datetime | None:
    try:
        d = datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None
    if d is None:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def same_instant_ms(sent: Any, got: Any) -> bool:
    """The cloud stores times to the millisecond: what we sent, cut to the millisecond, must be what it holds."""
    a, b = _instant(sent), _instant(got)
    return a is not None and b is not None and a.replace(microsecond=a.microsecond // 1000 * 1000) == b


def compare_with_cloud(bundle: dict[str, Any], g: dict, data: Any, html: str, versions: Any,
                       old_html: dict[int, str], events: Any) -> list[str]:
    """Everything the cloud stores of this page, compared with what was sent, TYPE-STRICT. Returns the names of
    the parts that differ ([] = identical). Pure and CPU-bound: call it in a worker thread."""
    bad: list[str] = []
    meta = bundle["meta"]
    if g.get("role") != "owner":
        bad.append("owner")
    if g.get("status") not in ("active", "read_only"):
        bad.append(f"status {g.get('status')}")
    if g.get("title") != " ".join(meta["title"].split()):
        bad.append("title")
    if g.get("description") != meta.get("description"):
        bad.append("description")
    if g.get("data_notes") != meta.get("data_notes"):
        bad.append("data notes")
    if g.get("archived") is not bool(meta.get("archived")):
        bad.append("archived")
    if g.get("version") != bundle["html_version"] or isinstance(g.get("version"), bool):
        bad.append("page version")
    try:
        if canon(_norm_schemas(g.get("schemas"))) != canon(_norm_schemas(bundle["schemas"])):
            bad.append("collection schemas")
    except (ValueError, TypeError):
        bad.append("collection schemas")
    if not isinstance(data, dict) or not isinstance(data.get("rev"), int):
        return bad + ["data"]
    try:
        if canon(data.get("state")) != canon(bundle["state"]):
            bad.append("state")
        if data.get("state_version") != bundle["state_version"]:
            bad.append("state version")
        got_docs = {name: {d.get("id"): d for d in docs} for name, docs in (data.get("collections") or {}).items() if docs}
        sent_docs = {name: docs for name, docs in bundle["collections"].items() if docs}
        if {k: sorted(v) for k, v in got_docs.items()} != {k: sorted(v) for k, v in sent_docs.items()}:
            bad.append("document counts")
        else:
            differ = times = False
            for name, docs in sent_docs.items():
                for doc_id, d in docs.items():
                    theirs = got_docs[name][doc_id]
                    if canon(theirs.get("data")) != canon(d["data"]):
                        differ = True
                    if not (same_instant_ms(d["created_at"], theirs.get("created_at"))
                            and same_instant_ms(d.get("updated_at") or d["created_at"], theirs.get("updated_at"))):
                        times = True
            if differ:
                bad.append("documents")
            if times:
                bad.append("document times")
    except (ValueError, TypeError):
        bad.append("data")
    if html != bundle["html"]:
        bad.append("page")
    # page history: the same versions, the older ones with the same html, note and time
    older = sorted(bundle.get("html_versions") or [], key=lambda v: v["version"])
    got_versions = {v.get("version"): v for v in (versions or {}).get("versions", [])} if isinstance(versions, dict) else {}
    if sorted(got_versions) != sorted([v["version"] for v in older] + [bundle["html_version"]]):
        bad.append("page versions")
    else:
        for v in older:
            theirs = got_versions[v["version"]]
            if old_html.get(v["version"]) != v["html"]:
                bad.append("page versions")
                break
            if theirs.get("note") != v.get("note") or not same_instant_ms(v["created_at"], theirs.get("created_at")):
                bad.append("page version times")
                break
    sent_events = bundle["events"]
    got_events = (events or {}).get("events") if isinstance(events, dict) else None
    if not isinstance(got_events, list) or len(got_events) != len(sent_events):
        bad.append("events")
    else:
        try:
            if canon([[e["name"], e.get("payload") or {}] for e in sent_events]) != \
                    canon([[e.get("name"), e.get("payload") or {}] for e in got_events]):
                bad.append("events")
            elif not all(same_instant_ms(a["created_at"], b.get("created_at")) for a, b in zip(sent_events, got_events)):
                bad.append("event times")
        except (ValueError, TypeError):
            bad.append("events")
    return bad


def _norm_schemas(value: Any) -> dict:
    out = {}
    for name, spec in (value or {}).items():
        try:
            out[name] = s.CollectionSchema.model_validate(spec).model_dump(exclude_none=True)
        except ValueError:
            out[name] = spec
    return out


# ── wire format ──────────────────────────────────────────────────────────────
def pack(bundle: dict[str, Any]) -> bytes:
    """Compact JSON, gzip'd. Raises BundleInvalid for a value JSON cannot carry (NaN, Infinity)."""
    try:
        raw = json.dumps(bundle, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    except (ValueError, TypeError):
        raise BundleInvalid("This page holds a value that cannot be sent (NaN or Infinity).") from None
    return gzip.compress(raw, 6, mtime=0)


def _reject_constant(name: str):
    raise ValueError(f"{name} is not allowed")


def unpack(data: bytes, cap: int | None = None) -> dict[str, Any]:
    """gunzip with a ceiling that holds WHILE inflating, then strict JSON. Raises BundleInvalid."""
    cap = settings.cloud_max_bundle_bytes if cap is None else cap
    if data[:2] != b"\x1f\x8b" and data.lstrip()[:1] == b"{":
        # Plain JSON: a transport that already undid `Content-Encoding: gzip`. Same ceiling, no inflating.
        raw = data
        if len(raw) > cap:
            raise BundleInvalid("The cloud's download is larger than this server accepts.", {"max_bytes": cap})
    else:
        inflater = zlib.decompressobj(wbits=16 + zlib.MAX_WBITS)
        try:
            raw = inflater.decompress(data, cap + 1)
        except zlib.error:
            raise BundleInvalid("The cloud's download is not a gzip file.") from None
        if len(raw) > cap or inflater.unconsumed_tail:
            raise BundleInvalid("The cloud's download is larger than this server accepts.", {"max_bytes": cap})
        if not inflater.eof:
            raise BundleInvalid("The cloud's download is cut short.")
    try:
        bundle = json.loads(raw, parse_constant=_reject_constant)
    except (ValueError, RecursionError, UnicodeDecodeError):
        raise BundleInvalid("The cloud's download is not valid JSON.") from None
    if not isinstance(bundle, dict):
        raise BundleInvalid("The cloud's download is not a bundle.")
    return bundle


# ── bundle -> local ──────────────────────────────────────────────────────────
def _when(value: Any, where: str) -> str:
    try:
        d = datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        d = None
    if d is None:
        raise BundleInvalid(f"{where} is not a date and time.")
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc).isoformat()


def bundle_to_snapshot(
    bundle: dict[str, Any], *, user_id: str, artifact_id: str, cloud_rev: int, position: int | None,
    updated_at: datetime, prior: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A bundle the cloud exported as a local snapshot (the rows to write). It must be a `full`
    bundle that conforms to the schema, and respect every limit a local artifact has.

    `prior` is the sealed backup taken when this artifact was moved, if one is still around. It is a
    FALLBACK only: it supplies what a bundle cannot carry (crew, chat and alert links, when the
    page was last opened, and the create time/note of the current page version when that page is
    unchanged). Nothing the cloud sent is ever replaced by it."""
    errors = validate_bundle(bundle)
    if errors:
        raise BundleInvalid("The cloud's download does not match the bundle format.", {"errors": errors})
    if bundle["kind"] != "full":
        raise BundleInvalid("The cloud's download is not a full bundle.")
    meta = bundle["meta"]
    html = bundle["html"]
    if len(html.encode()) > s.MAX_HTML_BYTES:
        raise BundleInvalid("The page is larger than this server allows.")
    state = bundle.get("state") or {}
    if _size(state) > s.MAX_STATE_BYTES:
        raise BundleInvalid("The state is larger than this server allows.")
    schemas: dict[str, Any] = {}
    for name, spec in (bundle.get("schemas") or {}).items():
        try:
            schemas[name] = s.CollectionSchema.model_validate(spec).model_dump(exclude_none=True)
        except ValueError:
            raise BundleInvalid(f"The schema of collection {name!r} is not valid here.") from None

    docs: list[dict[str, Any]] = []
    for name, items in (bundle.get("collections") or {}).items():
        if len(items) > s.MAX_DOCS_PER_COLLECTION:
            raise BundleInvalid(f"Collection {name!r} has more documents than this server allows.")
        for doc_id, d in items.items():
            if _size(d["data"]) > s.MAX_DOC_BYTES:
                raise BundleInvalid(f"Document {name}/{doc_id} is larger than this server allows.")
            created = _when(d["created_at"], f"{name}/{doc_id} created_at")
            docs.append({"collection": name, "doc_id": doc_id, "version": 1, "created_at": created,
                         "updated_at": _when(d["updated_at"], f"{name}/{doc_id} updated_at") if d.get("updated_at") else created,
                         "data": d["data"]})
    docs.sort(key=lambda d: (d["collection"], d["created_at"], d["doc_id"]))
    events = []
    for i, e in enumerate(bundle.get("events") or [], 1):
        payload = e.get("payload") or {}
        if _size(payload) > s.MAX_DOC_BYTES:
            raise BundleInvalid(f"Event {i} is larger than this server allows.")
        events.append({"seq": None, "name": e["name"], "created_at": _when(e["created_at"], "an event time"),
                       "handled": 0, "payload": payload})

    older = sorted(bundle.get("html_versions") or [], key=lambda v: v["version"])
    current = bundle.get("html_version") or ((older[-1]["version"] + 1) if older else 1)
    if older and current <= older[-1]["version"]:
        raise BundleInvalid("html_version must be newer than every older page version.")
    if len({v["version"] for v in older}) != len(older):
        raise BundleInvalid("Page versions must have distinct numbers.")
    versions = []
    for v in older:
        if len(v["html"].encode()) > s.MAX_HTML_BYTES:
            raise BundleInvalid("An older page version is larger than this server allows.")
        versions.append({"version": v["version"], "created_at": _when(v["created_at"], "a page version time"),
                         "note": v.get("note"), "html": v["html"]})
    prior_art = (prior or {}).get("artifact") or {}
    prior_row = next((v for v in (prior or {}).get("versions", [])
                      if v["version"] == current and v["html"] == html), None)
    versions.append({
        "version": current, "html": html,
        "created_at": prior_row["created_at"] if prior_row else updated_at.isoformat(),
        "note": prior_row["note"] if prior_row else "from the cloud"})

    prior_doc = prior_art.get("doc") or {}
    doc = ArtifactDoc(
        id=artifact_id, user_id=user_id, title=meta["title"], description=meta.get("description"),
        crew=meta.get("crew") or prior_doc.get("crew"), session_id=prior_doc.get("session_id"),
        alert_id=prior_doc.get("alert_id"), data_notes=meta.get("data_notes"), schemas=schemas,
        created_at=datetime.fromisoformat(_when(meta.get("created_at") or updated_at.isoformat(), "created_at")),
        last_opened_at=prior_doc.get("last_opened_at"),
    ).model_dump(mode="json")
    state_version = bundle.get("state_version")
    return {
        "format": 1,
        "artifact": {
            "id": artifact_id, "user_id": user_id, "archived": bool(meta.get("archived")), "version": current,
            "state": state, "state_version": state_version if state_version is not None else (1 if state else 0),
            # The cloud's own counter, plus one: a page polling `since_rev=N` must always see this as news.
            "rev": max(cloud_rev, int(prior_art.get("rev") or 0)) + 1,
            "updated_at": updated_at.isoformat(), "position": position, "doc": doc, "html": html,
        },
        "versions": versions, "docs": docs, "events": events,
    }
