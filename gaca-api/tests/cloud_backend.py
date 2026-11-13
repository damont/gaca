"""A stateful fake of the GACA Cloud's transfer routes AND its S3 bucket, for the move / bring-home tests.

`FakeBackend` extends `FakeCloud` (tests/cloud_support.py): paths it does not implement fall back to the
scripted `.on(...)` answers, so one object serves the transfer routes and the plain pass-throughs.
Requests for the S3 host (`bucket.s3.amazonaws.com`) are answered by the fake bucket; any other host is
a connection error, which is exactly what the SSRF tests want to see never happen.

It follows the real cloud (gaca-cloud backend/api/services/bundles.py, bring_home.py) where it matters:
  * import: reads the uploaded gzip, validates it against the bundle schema, keeps `meta.origin_id` as the id
    (409 `conflict` if the caller owns it, 409 `id_conflict` if another account does), keeps the 4 older page
    versions + the current one, renumbers events, resets document versions to 1, truncates timestamps to ms,
    answers 202 with the gacabit; a consumed upload id is 404 afterwards; 503 releases the upload;
  * bring-home: needs `user_confirmed` from a server, flips the gacabit to `brought_home`, writes the export to
    the bucket and returns a presigned GET; a second bring-home is `409 conflict` with `undo_until`;
    confirm / undo as in the contract;
  * the bucket only takes a PUT that carries exactly the declared headers and size, and no Authorization.

Faults are injected with `faults[name] = [...]`, each entry used once, in order.
"""
from __future__ import annotations

import gzip
import json
import re
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import jsonschema

from gaca_api.services import cloud_bundle
from tests.cloud_support import FakeCloud

S3_HOST = "bucket.s3.amazonaws.com"
ACCOUNT = {"id": "acct_david", "handle": "david"}
_SCHEMA = json.loads(cloud_bundle._SCHEMA_PATH.read_text())  # noqa: SLF001


def _now() -> datetime:
    n = datetime.now(timezone.utc)
    return n.replace(microsecond=n.microsecond // 1000 * 1000)


def _ms(value: str) -> str:
    d = datetime.fromisoformat(value)
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    d = d.astimezone(timezone.utc)
    return d.replace(microsecond=d.microsecond // 1000 * 1000).isoformat()


def bad_int(value) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return not -(2 ** 63) <= value < 2 ** 63
    if isinstance(value, dict):
        return any(bad_int(v) for v in value.values())
    if isinstance(value, list):
        return any(bad_int(v) for v in value)
    return False


def err(status: int, code: str, message: str, detail: dict | None = None, headers: dict | None = None):
    body = {"code": code, "message": message} | ({"detail": detail} if detail else {})
    return httpx.Response(status, json=body, headers=headers or {})


class FakeBackend(FakeCloud):
    def __init__(self) -> None:
        super().__init__()
        self.gacabits: dict[str, dict] = {}
        self.foreign_ids: set[str] = set()  # ids held by another account
        self.uploads: dict[str, dict] = {}
        self.bucket: dict[str, bytes] = {}
        self.s3_requests: list[httpx.Request] = []
        self.imports = 0  # imports that created a gacabit
        self.faults: dict[str, list] = {}
        self.other_hosts: list[str] = []
        self.s3_url_host = S3_HOST  # what the presign names (SSRF tests change it)
        self.s3_url_scheme = "https"
        self.s3_url_suffix = ""  # e.g. "@evil.example.com" appended after the host
        self.presign_headers: dict | None = None
        self.on_confirm = None  # called (no args) when a bring-home confirm arrives
        self.billing_required = False  # uploads and imports answer 402 (the create gate of the real cloud)
        self.upload_limit, self.transfer_limit = 20, 30  # per account per hour, as in config_bundles.py
        self.upload_hits = self.transfer_hits = 0
        self.serve_list = False  # answer GET /gacabits from the stored gacabits instead of a script
        self.tamper = None  # called with the stored gacabit right after an import (simulates a cloud that drifts)

    # ── plumbing ─────────────────────────────────────────────────────────────
    def fault(self, name: str):
        queue = self.faults.get(name)
        return queue.pop(0) if queue else None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == S3_HOST:
            self.s3_requests.append(request)
            return self._s3(request)
        if host != "cloud.test":
            self.other_hosts.append(host)
            raise httpx.ConnectError("no route to " + host, request=request)
        if self.down:
            self.requests.append(request)
            raise httpx.ConnectError("boom", request=request)
        path = request.url.path.removeprefix("/api/v1")
        self.requests.append(request)  # recorded before it is answered: a lost answer was still a request
        handled = self._api(request, path)
        if handled is not None:
            return handled
        self.requests.pop()
        return super().__call__(request)

    def body(self, request: httpx.Request) -> dict:
        return json.loads(request.content or b"{}")

    # ── representations ──────────────────────────────────────────────────────
    def out(self, g: dict) -> dict:
        return {
            "id": g["id"], "title": g["title"], "description": g["description"], "data_notes": g["data_notes"],
            "schemas": g["schemas"], "archived": g["archived"], "version": g["version"], "rev": g["rev"],
            "state_version": g["state_version"], "owner": ACCOUNT, "role": "owner", "status": g["status"],
            "member_count": g.get("member_count", 1), "size_bytes": 1000, "created_at": g["created_at"], "updated_at": g["updated_at"],
            "link": f"gaca://gacabit/{g['id']}", "brought_home_until": g.get("undo_until"),
        }

    def export_bundle(self, g: dict) -> dict:
        collections: dict = {}
        for (coll, doc_id), d in sorted(g["docs"].items(), key=lambda kv: (kv[1]["created_at"], kv[0][1])):
            collections.setdefault(coll, {})[doc_id] = {
                "data": d["data"], "created_at": d["created_at"], "updated_at": d["updated_at"]}
        meta = {"title": g["title"], "archived": g["archived"], "created_at": g["created_at"], "origin_id": g["id"]}
        for k in ("description", "data_notes"):
            if g[k] is not None:
                meta[k] = g[k]
        bundle = {
            "bundle_version": 1, "kind": "full", "meta": meta, "html": g["html"], "html_version": g["version"],
            "schemas": g["schemas"], "state": g["state"], "state_version": g["state_version"],
            "collections": collections,
            "events": [{"name": e["name"], "created_at": e["created_at"], "payload": e["payload"]}
                       for e in g["events"] if not e["handled"]],
            "exported_at": _now().isoformat(),
        }
        older = [v for v in g["versions"] if v["version"] != g["version"]]
        if older:
            bundle["html_versions"] = [{"version": v["version"], "created_at": v["created_at"], "note": v["note"],
                                        "html": v["html"]} for v in older]
        return bundle

    # ── the API ──────────────────────────────────────────────────────────────
    def _api(self, request: httpx.Request, path: str) -> httpx.Response | None:
        m, rx = request.method, re.fullmatch
        if (m, path) == ("GET", "/me"):
            return httpx.Response(200, json={**ACCOUNT, "email": "d@example.com", "created_at": "x"})
        if (m, path) == ("GET", "/gacabits") and self.serve_list:
            return httpx.Response(200, json={"gacabits": [self.out(g) for g in self.gacabits.values()],
                                             "next_cursor": None})
        if (m, path) == ("POST", "/uploads"):
            return self._uploads(request)
        if (m, path) == ("POST", "/gacabits/import"):
            return self._import(request)
        hit = rx(r"/gacabits/([0-9a-f]{32})(/.*)?", path)
        if hit is None:
            return None
        gid, tail = hit.group(1), hit.group(2) or ""
        g = self.gacabits.get(gid)
        if (m, tail) == ("GET", "") or (g is not None and (m, tail) in (
                ("GET", "/data"), ("GET", "/html"), ("GET", "/events"), ("GET", "/versions"))):
            if g is None:
                return err(404, "not_found", "gacabit not found")
            fault = self.fault("read_after_import") if self.imports else None
            if fault == "network":
                raise httpx.ReadTimeout("slow", request=request)
            if tail == "":
                return httpx.Response(200, json=self.out(g))
            if tail == "/html":
                want = request.url.params.get("version")
                if want is not None:
                    row = next((v for v in g["versions"] if str(v["version"]) == want), None)
                    if row is None:
                        return err(404, "not_found", "no such version")
                    return httpx.Response(200, text=row["html"], headers={"content-type": "text/html"})
                return httpx.Response(200, text=g["html"], headers={"content-type": "text/html"})
            if tail == "/versions":
                return httpx.Response(200, json={"current": g["version"], "versions": [
                    {"version": v["version"], "created_at": v["created_at"], "note": v["note"], "size": len(v["html"])}
                    for v in g["versions"]]})
            if tail == "/events":
                unhandled = request.url.params.get("unhandled") == "true"
                return httpx.Response(200, json={"events": [
                    {"seq": e["seq"], "name": e["name"], "created_at": e["created_at"], "handled": e["handled"],
                     "payload": e["payload"]} for e in g["events"] if not (unhandled and e["handled"])]})
            collections: dict = {}
            for (coll, doc_id), d in sorted(g["docs"].items(), key=lambda kv: (kv[1]["created_at"], kv[0][1])):
                collections.setdefault(coll, []).append({"id": doc_id, "version": 1, **d})
            data = {"rev": g["rev"], "changed": True, "state": g["state"], "state_version": g["state_version"],
                    "collections": collections, "page_version": g["version"], "role": "owner"}
            if self.faults.get("data_tamper"):
                data["state"] = {**g["state"], "tampered": True}
            return httpx.Response(200, json=data)
        if tail.startswith("/bring-home"):
            return self._bring_home(request, g, tail)
        return None

    def _uploads(self, request: httpx.Request) -> httpx.Response:
        fault = self.fault("uploads")
        if fault:
            return err(*fault)
        if self.billing_required:
            return err(402, "billing_required", "Set up billing to create more gacabits")
        self.upload_hits += 1
        if self.upload_hits > self.upload_limit:
            return err(429, "rate_limited", "Too many transfers; try again later", headers={"Retry-After": "1800"})
        body = self.body(request)
        if body.get("purpose") != "bundle" or not isinstance(body.get("size_bytes"), int):
            return err(422, "request_invalid", "bad request")
        if body["size_bytes"] > 16 * 1024 * 1024:
            return err(413, "payload_too_large", "A bundle upload is at most 16777216 bytes")
        upload_id = uuid.uuid4().hex
        key = f"uploads/acct_david/{upload_id}.json.gz"
        self.uploads[upload_id] = {"key": key, "size": body["size_bytes"], "consumed": False}
        url = (f"{self.s3_url_scheme}://{self.s3_url_host}{self.s3_url_suffix}/bundles/{key}"
               f"?X-Amz-Signature=SECRETSIG{upload_id}&X-Amz-Expires=900")
        headers = self.presign_headers if self.presign_headers is not None else {
            "Content-Type": "application/json", "Content-Encoding": "gzip"}
        return httpx.Response(201, json={"upload_id": upload_id, "url": url, "method": "PUT", "headers": headers,
                                         "expires_in": 900})

    def _import(self, request: httpx.Request) -> httpx.Response:
        fault = self.fault("import")
        if isinstance(fault, tuple):
            return err(*fault)
        if self.billing_required:
            return err(402, "billing_required", "Set up billing to create more gacabits")
        self.transfer_hits += 1
        if self.transfer_hits > self.transfer_limit:
            return err(429, "rate_limited", "Too many transfers; try again later", headers={"Retry-After": "1800"})
        upload_id = self.body(request).get("upload_id")
        up = self.uploads.get(upload_id)
        if up is None or up["consumed"]:
            return err(404, "not_found", "Upload not found")
        data = self.bucket.get("bundles/" + up["key"])
        if data is None:
            return err(422, "bundle_invalid", "Nothing has been uploaded to this upload id yet")
        try:
            raw = gzip.decompress(data)
            bundle = json.loads(raw)
            jsonschema.Draft202012Validator(_SCHEMA).validate(bundle)
        except Exception as e:  # noqa: BLE001
            return err(422, "bundle_invalid", "The bundle does not match the bundle format", {"errors": [str(e)[:80]]})
        if len(raw) > 12 * 1024 * 1024:
            return err(413, "gacabit_size_exceeded", "too big")
        if bad_int(bundle):  # the real cloud's database cannot hold these (check_json)
            return err(422, "bundle_invalid", "A number does not fit in 64 bits")
        origin = bundle["meta"].get("origin_id")
        if origin in self.foreign_ids:
            return err(409, "id_conflict", "This gacabit's id is already used in the cloud by another account")
        if origin in self.gacabits:
            return err(409, "conflict", "This gacabit is already in your cloud", {"gacabit_id": origin})
        if fault == "lose":
            up["consumed"] = True
            self._make(bundle)
            raise httpx.ReadTimeout("the answer was lost", request=request)
        up["consumed"] = True
        return httpx.Response(202, json=self.out(self._make(bundle)))

    def _make(self, bundle: dict) -> dict:
        now = _now()
        meta = bundle["meta"]
        gid = meta["origin_id"]
        older = sorted(bundle.get("html_versions") or [], key=lambda v: v["version"])[-4:]
        current = bundle.get("html_version") or ((older[-1]["version"] + 1) if older else 1)
        versions = [{**v, "created_at": _ms(v["created_at"])} for v in older]
        versions.append({"version": current, "created_at": now.isoformat(), "note": "imported", "html": bundle["html"]})
        docs = {}
        for coll, items in (bundle.get("collections") or {}).items():
            for doc_id, d in items.items():
                made = _ms(d["created_at"])
                docs[(coll, doc_id)] = {"created_at": made, "updated_at": _ms(d.get("updated_at") or made), "data": d["data"]}
        events = [{"seq": i, "name": e["name"], "created_at": _ms(e["created_at"]), "handled": False,
                   "payload": e.get("payload") or {}} for i, e in enumerate(bundle.get("events") or [], 1)]
        state = bundle.get("state") or {}
        g = {
            "id": gid, "title": meta["title"], "description": meta.get("description"),
            "data_notes": meta.get("data_notes"), "schemas": bundle.get("schemas") or {},
            "archived": bool(meta.get("archived")), "version": current, "html": bundle["html"], "versions": versions,
            "state": state, "state_version": bundle.get("state_version", 1 if state else 0), "docs": docs,
            "events": events, "rev": 0, "status": "active", "created_at": _ms(meta.get("created_at") or now.isoformat()),
            "updated_at": now.isoformat(), "confirmed": False, "undo_until": None,
        }
        if self.tamper:
            self.tamper(g)
        self.gacabits[gid] = g
        self.imports += 1
        return g

    def _bring_home(self, request: httpx.Request, g: dict | None, tail: str) -> httpx.Response:
        if g is None:
            return err(404, "not_found", "gacabit not found")
        if tail == "/bring-home":
            if request.method != "POST":
                return err(405, "bad_request", "no")
            fault = self.fault("bring_home")
            if isinstance(fault, tuple):
                return err(*fault)
            if not self.body(request).get("user_confirmed"):
                return err(403, "user_confirmation_required", "needs user_confirmed")
            self.transfer_hits += 1
            if self.transfer_hits > self.transfer_limit:
                return err(429, "rate_limited", "Too many transfers; try again later", headers={"Retry-After": "1800"})
            if g["status"] == "brought_home":
                return err(409, "conflict", "This gacabit was already brought home; undo that first to change it",
                           {"undo_until": g["undo_until"]})
            g["status"] = "brought_home"
            g["undo_until"] = (_now() + timedelta(days=7)).isoformat()
            key = f"exports/acct_david/{uuid.uuid4().hex}.json.gz"
            data = gzip.compress(json.dumps(self.export_bundle(g)).encode(), mtime=0)
            if fault == "corrupt":
                data = b"not gzip at all"
            if not self.fault("export_vanish"):
                self.bucket["bundles/" + key] = data
            url = (f"{self.s3_url_scheme}://{self.s3_url_host}{self.s3_url_suffix}/bundles/{key}"
                   f"?X-Amz-Signature=SECRETSIG&X-Amz-Expires=300")
            return httpx.Response(200, json={"download_url": url, "expires_in": 300, "bundle_version": 1,
                                             "undo_until": g["undo_until"], "needs_confirmation": True})
        if tail == "/bring-home/confirm":
            fault = self.fault("confirm")
            if fault == "network":
                raise httpx.ConnectError("down", request=request)
            if g["status"] != "brought_home":
                return err(409, "conflict", "This gacabit has not been brought home")
            if self.on_confirm:
                self.on_confirm()
            g["confirmed"] = True
            return httpx.Response(204)
        if tail == "/bring-home/undo":
            if g["status"] != "brought_home":
                return err(409, "conflict", "This gacabit has not been brought home")
            if self.faults.get("undo_closed"):
                return err(410, "undo_window_closed", "The undo window has closed")
            g["status"], g["undo_until"], g["confirmed"] = "active", None, False
            return httpx.Response(200, json=self.out(g))
        return None

    # ── the bucket ───────────────────────────────────────────────────────────
    def _s3(self, request: httpx.Request) -> httpx.Response:
        key = request.url.path.lstrip("/")
        if self.faults.get("s3_redirect"):
            return httpx.Response(302, headers={"location": "https://evil.example.com/x"})
        if request.method == "PUT":
            fault = self.fault("s3_put")
            if fault == "network":
                raise httpx.ConnectError("reset", request=request)
            if isinstance(fault, int):
                return httpx.Response(fault, text="<Error/>")
            assert "authorization" not in request.headers, "the bucket must never see the cloud token"
            assert "x-request-id" not in request.headers
            if request.headers.get("content-type") != "application/json" or request.headers.get("content-encoding") != "gzip":
                return httpx.Response(403, text="<Error>SignatureDoesNotMatch</Error>")
            upload_id = key.rsplit("/", 1)[-1].removesuffix(".json.gz")
            up = self.uploads.get(upload_id)
            if up is None or len(request.content) != up["size"]:
                return httpx.Response(403, text="<Error>SignatureDoesNotMatch</Error>")
            self.bucket[key] = bytes(request.content)
            return httpx.Response(200)
        if request.method == "GET":
            data = self.bucket.get(key)
            if data is None:
                return httpx.Response(404, text="<Error>NoSuchKey</Error>")
            headers = {"content-type": "application/json"}
            if data[:2] == b"\x1f\x8b":
                headers["content-encoding"] = "gzip"  # what S3 sends for an object stored with ContentEncoding gzip
            return httpx.Response(200, content=data, headers=headers)
        return httpx.Response(405)
