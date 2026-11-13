"""Moving a page to the cloud, and bringing it home (Part 8b). The person taps "Move to cloud" /
"Bring home" in the app; that tap is the confirmation, so every entry point here is reached only
from routes that require a PAIRED DEVICE (an API token, i.e. Hermes, can never get here).

THE RULE: no local content is deleted until the cloud copy has been verified, and when it is deleted
a sealed backup of it is written in the SAME transaction.

Move to the cloud (`move_to_cloud`), phases in order:

  preflight   linked, not archived, id portable, the bundle fits the cloud's caps (413 before anything is
              sent), the cloud answers. The artifact is read-only (423) from here until the end.
  uploading   POST /uploads -> PUT the gzip bundle to the presigned S3 URL (services/cloud_transfer.py)
  importing   POST /gacabits/import {upload_id}. 503 / a lost answer: retried with the SAME upload id;
              before each retry the cloud is asked whether the gacabit is already there, and if it is
              we carry on instead of failing (the import worked, only its answer was lost)
  verifying   GET the cloud's copy (page, state, every document, events) and compare with what was sent
  converting  ONE local transaction: sealed backup row + delete local rows + create the pointer

A failure in any phase leaves the local artifact exactly as it was and says which phase and why
(`{code, message, detail: {phase, ...}}`). Retrying is always safe: a cloud copy from an earlier
try is found, verified against the CURRENT local content, and used (or refused if it differs).

Bring home (`bring_home`): ask the cloud (`/bring-home`, user_confirmed), download + validate the bundle,
restore the artifact locally in ONE transaction (replacing the pointer), THEN tell the cloud it arrived
(`/bring-home/confirm`; until then the cloud never deletes its copy). If anything fails before the local
restore, the cloud's bring-home is undone so the cloud copy is as it was, the pointer stays, and the
error says so. If only the final confirm fails the page is home and the confirm is retried later.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Any

from ..config import settings
from ..db import artifacts as artifacts_db
from ..db import cloud as cloud_db
from ..db import cloud_move as db
from ..models.base import new_id, utcnow
from ..schemas import artifact as s
from . import artifact_locks, cloud_bundle, cloud_client, cloud_link, cloud_list, cloud_transfer
from . import artifacts as artifact_service
from .exceptions import CloudError, CloudReply, CloudUnavailable, NotFound

logger = logging.getLogger(__name__)
_JSON = {"content-type": "application/json"}
_MAINTENANCE_EVERY = timedelta(hours=1)
_last_maintenance: datetime | None = None


# ── cloud answers -> this server's error body ────────────────────────────────
def _parse(reply: CloudReply) -> dict:
    try:
        data = json.loads(reply.body or b"{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _reply_code(reply: CloudReply) -> str:
    code = _parse(reply).get("code")
    return code if isinstance(code, str) else ""


def _map(reply: CloudReply, phase: str) -> CloudError:
    """The cloud's refusal as `{code, message, detail}`: same status, the cloud's own code and message
    (413 is always `too_large_for_cloud`), and its structured detail (e.g. the validation findings)."""
    data = _parse(reply)
    cloud_code = data.get("code") if isinstance(data.get("code"), str) else None
    message = data.get("message") if isinstance(data.get("message"), str) else "The cloud refused the request."
    detail: dict[str, Any] = {"phase": phase, "cloud_code": cloud_code}
    structured = data.get("info") if isinstance(data.get("info"), dict) else data.get("detail")
    if isinstance(structured, dict):
        detail["cloud"] = structured
    headers = {k: v for k, v in reply.headers.items() if k.lower() == "retry-after"}
    code = "too_large_for_cloud" if reply.status == 413 else (cloud_code or "cloud_error")
    return CloudError(message, status=reply.status, code=code, detail=detail, headers=headers or None)


def _json(resp) -> Any:
    return cloud_link._json(resp)  # noqa: SLF001


async def _call(user_id: str, method: str, path: str, *, body: Any = None, request_id: str | None = None):
    return await cloud_link.call(
        user_id, method, path, request_id=request_id,
        content=None if body is None else json.dumps(body).encode(), headers=None if body is None else _JSON)


async def _get_gacabit(user_id: str, gid: str, request_id: str | None = None) -> dict | None:
    """The cloud's metadata for this id as the linked account sees it, or None when it has none (404)."""
    try:
        resp = await _call(user_id, "GET", f"/gacabits/{cloud_client.quote_id(gid)}", request_id=request_id)
    except CloudReply as r:
        if r.status == 404:
            return None
        raise
    g = _json(resp)
    if not isinstance(g, dict) or g.get("id") != gid:
        raise CloudUnavailable("The cloud answered in a way this server did not understand.")
    return g


def _delay(reply: CloudReply | None = None) -> float:
    after = (reply.headers.get("retry-after") if reply else None)
    try:
        return min(max(float(after), 0.0), 30.0) if after is not None else settings.cloud_retry_pause_seconds
    except ValueError:
        return settings.cloud_retry_pause_seconds


# ═════════════════════════ move to the cloud ═════════════════════════════════
class _Phase:
    name = "preflight"
    known = False  # the artifact exists locally (a failure is worth remembering for the app)


async def move_to_cloud(
    user_id: str, artifact_id: str, request_id: str | None = None, acknowledge_not_carried: bool = False,
) -> dict[str, Any]:
    link = await cloud_link.require_link(user_id)
    if link.scope != "manage":
        raise CloudError("This server's cloud link is read-only. Link it again with full access to move pages.",
                         status=403, code="cloud_link_read_only")
    if await cloud_db.get_pointer(user_id, artifact_id) is not None:
        raise CloudError("This page already lives in the cloud.", status=409, code="already_in_cloud")
    phase = _Phase()
    with artifact_locks.busy(artifact_id, moving=True):
        try:
            return await _move(user_id, artifact_id, link, phase, request_id, acknowledge_not_carried)
        except CloudReply as r:
            err = _map(r, phase.name)
        except CloudUnavailable as e:
            err = CloudError(e.message, status=e.status, code=e.code)
        except CloudError as e:
            err = e
        except cloud_bundle.BundleInvalid as e:
            err = CloudError(str(e), status=422, code="bundle_invalid", detail=e.detail)
        except NotFound:
            raise
        except Exception:  # noqa: BLE001 - a bug or a disk problem: still a clear answer, and nothing was changed
            logger.exception("move to the cloud failed unexpectedly")
            err = CloudError("Moving the page failed unexpectedly. It was not changed here; try again.",
                             status=500, code="move_failed")
        # Only a refusal gets here. Say which phase, and remember it so the app can show a retry.
        if not isinstance(err.detail, dict):
            err.detail = {}
        err.detail.setdefault("phase", phase.name)
        err.detail["local_data_changed"] = False
        if phase.known and err.code != "confirmation_required":
            await db.set_failure(user_id, artifact_id, phase.name, err.code, err.message, utcnow())
        raise err


def _prepare(snap: dict[str, Any], now: datetime) -> dict[str, Any]:
    """All the CPU work of a move, in one plain function so it can run in a worker thread (a 12 MB page takes
    seconds of JSON and gzip that must not stall every other request): the bundle, what stops it fitting, the
    numbers the cloud would refuse, the wire bytes, the schema check, the fingerprint, what is not carried."""
    bundle = cloud_bundle.build_bundle(snap, now)
    problems = cloud_bundle.too_big(snap, bundle)
    numbers = cloud_bundle.bad_numbers(bundle)
    out: dict[str, Any] = {"bundle": bundle, "problems": problems, "numbers": numbers, "data": None, "errors": [],
                           "fingerprint": db.fingerprint(snap), "not_carried": cloud_bundle.not_carried(snap)}
    if problems or numbers:
        return out
    out["data"] = cloud_bundle.pack(bundle)
    out["errors"] = cloud_bundle.validate_bundle(bundle)
    return out


def _preflight_errors(prep: dict[str, Any]) -> CloudError | None:
    if prep["numbers"]:
        return CloudError(
            "This page holds numbers the cloud cannot store (whole numbers beyond 64 bits, or NaN/Infinity). "
            "Nothing was sent; change them and try again.", status=422, code="unsupported_number",
            detail={"paths": prep["numbers"]})
    if prep["problems"]:
        return CloudError("This page is too big for the cloud. Nothing was sent; trim what is listed and try again.",
                          status=413, code="too_large_for_cloud", detail={"problems": prep["problems"]})
    data = prep["data"]
    if len(data) > settings.cloud_max_transfer_bytes:
        return CloudError("This page is too big for the cloud even compressed. Nothing was sent.", status=413,
                          code="too_large_for_cloud", detail={"problems": [{
                              "item": "the compressed bundle", "size": len(data),
                              "limit": settings.cloud_max_transfer_bytes, "unit": "bytes"}]})
    if prep["errors"]:  # our own bundle does not match the contract: a bug or data this server should not have
        return CloudError("This page cannot be put into the cloud's bundle format.", status=422,
                          code="bundle_invalid", detail={"errors": prep["errors"]})
    return None


async def move_preview(user_id: str, artifact_id: str) -> dict[str, Any]:
    """What a move would do, without doing anything and without calling the cloud: whether it fits, what is
    NOT carried over, and what happens to members (nobody: the page is not shared yet)."""
    if await cloud_db.get_pointer(user_id, artifact_id) is not None:
        raise CloudError("This page already lives in the cloud.", status=409, code="already_in_cloud")
    async with artifact_service._write_lock:  # noqa: SLF001
        snap = await db.read_snapshot(user_id, artifact_id)
    if snap is None:
        raise NotFound("artifact", artifact_id)
    prep = await asyncio.to_thread(_prepare, snap, utcnow())
    err = _preflight_errors(prep)
    problems = [] if err is None else (err.detail or {}).get("problems") or (
        [{"item": "numbers", "paths": prep["numbers"]}] if prep["numbers"] else [{"item": err.message}])
    raw = len(json.dumps(prep["bundle"], ensure_ascii=False).encode())
    carried = prep["not_carried"]
    return {
        "artifact_id": artifact_id,
        "can_move": err is None and not snap["artifact"]["archived"] and cloud_bundle.portable_id(artifact_id),
        "blocked_because": (None if err is None else err.code) or (
            "artifact_archived" if snap["artifact"]["archived"] else
            None if cloud_bundle.portable_id(artifact_id) else "id_not_portable"),
        "problems": problems,
        "size": {"bytes": raw, "limit_bytes": settings.cloud_max_bundle_bytes,
                 "compressed_bytes": len(prep["data"]) if prep["data"] else None,
                 "documents": len(snap["docs"]), "unhandled_events": len(prep["bundle"]["events"])},
        "not_carried": carried,
        "requires_acknowledgement": carried["anything"],
        "member_impact": "none",
        "backup_days": settings.cloud_backup_days,
    }


async def _move(user_id: str, artifact_id: str, link, phase: _Phase, request_id: str | None,
                acknowledge: bool = False) -> dict[str, Any]:
    async with artifact_service._write_lock:  # noqa: SLF001 - no write is half-done while we read
        snap = await db.read_snapshot(user_id, artifact_id)
    if snap is None:
        raise NotFound("artifact", artifact_id)
    phase.known = True
    art = snap["artifact"]
    if art["archived"]:
        raise CloudError("Archived pages cannot be moved to the cloud. Unarchive it first.",
                         status=409, code="artifact_archived")
    if not cloud_bundle.portable_id(artifact_id):
        raise CloudError("This page's id cannot be kept in the cloud, so it cannot be moved.",
                         status=422, code="id_not_portable")
    prep = await asyncio.to_thread(_prepare, snap, utcnow())
    refusal = _preflight_errors(prep)
    if refusal is not None:
        raise refusal
    bundle, data, fingerprint, carried = prep["bundle"], prep["data"], prep["fingerprint"], prep["not_carried"]
    if carried["anything"] and not acknowledge:
        raise CloudError(
            "Some of this page is not kept in the cloud. Review what is listed, then move it again with "
            "acknowledge_not_carried set to true. Nothing was sent.", status=409, code="confirmation_required",
            detail={"not_carried": carried, "backup_days": settings.cloud_backup_days})

    await _call(user_id, "GET", "/me", request_id=request_id)  # linked, token good, cloud reachable
    resumed = False
    existing = await _get_gacabit(user_id, artifact_id, request_id)
    if existing is not None:
        resumed = True
        _check_existing(existing, link)
    else:
        phase.name = "uploading"
        await _upload_and_import(user_id, artifact_id, data, phase, request_id)

    phase.name = "verifying"
    g = await _get_gacabit(user_id, artifact_id, request_id)
    if g is None:
        raise CloudError("The cloud does not show the copy that was just made. Nothing changed here; try again.",
                         status=502, code="verify_failed", detail={"mismatches": ["the copy is missing"]})
    mismatches = await _verify(user_id, artifact_id, bundle, g, request_id)
    if mismatches:
        if resumed:
            raise CloudError(
                "A copy of this page is already in your cloud account but differs from the page on this server. "
                "Nothing was changed. Remove that copy on the website (or bring it home) and try again.",
                status=409, code="cloud_copy_differs", detail={"mismatches": mismatches})
        raise CloudError("The cloud's copy did not match what was sent, so this page was NOT changed here.",
                         status=502, code="verify_failed", detail={"mismatches": mismatches})

    phase.name = "converting"
    backup_expires = await _convert(user_id, artifact_id, link, g, fingerprint)
    await db.clear_failure(artifact_id)
    cloud_list.forget(user_id)
    pointer = await cloud_db.get_pointer(user_id, artifact_id)
    assert pointer is not None
    return {"artifact": cloud_list._item(pointer),  # noqa: SLF001
            "verified": True, "resumed": resumed, "backup_expires_at": backup_expires.isoformat(),
            "not_carried": carried}


def _check_existing(g: dict, link) -> None:
    """A gacabit with this id is already in the linked account's cloud: usable only if it is ours and live."""
    owner = g.get("owner") if isinstance(g.get("owner"), dict) else {}
    if g.get("role") != "owner" or (owner.get("id") and link.account_id and owner.get("id") != link.account_id):
        raise CloudError("The cloud already has a different gacabit with this page's id.", status=409,
                         code="id_conflict")
    if g.get("status") not in ("active", "read_only"):
        raise CloudError(
            "The cloud copy of this page is being brought home or removed. Undo that on the website first.",
            status=409, code="cloud_copy_not_usable", detail={"cloud_status": g.get("status")})


async def _convert(user_id: str, artifact_id: str, link, g: dict, expected_fingerprint: str) -> datetime:
    """The single transaction that makes the page a cloud page. Re-reads the local rows under the write
    lock and refuses if they are not what was sent."""
    async with artifact_service._write_lock:  # noqa: SLF001
        snap = await db.read_snapshot(user_id, artifact_id)
        if snap is None:
            raise NotFound("artifact", artifact_id)
        if await asyncio.to_thread(db.fingerprint, snap) != expected_fingerprint:
            raise CloudError(
                "The page changed while it was being moved, so it was NOT switched to the cloud copy. Try again.",
                status=409, code="changed_during_move")
        pointer = cloud_list.pointer_from(user_id, link.account_id or "", g, snap["artifact"].get("position"))
        if pointer is None or pointer.role != "owner":
            raise CloudUnavailable("The cloud answered in a way this server did not understand.")
        now = utcnow()
        expires = now + timedelta(days=settings.cloud_backup_days)
        sealed = await asyncio.to_thread(db.seal, snap)  # gzip of the whole page: not on the event loop
        await db.convert_to_pointer(snap, pointer, backup_id=new_id(), created_at=now, expires_at=expires, sealed=sealed)
    return expires


async def _request_upload(user_id: str, size: int, request_id: str | None) -> dict:
    resp = await _call(user_id, "POST", "/uploads", body={"purpose": "bundle", "size_bytes": size},
                       request_id=request_id)
    t = _json(resp)
    if not (isinstance(t, dict) and isinstance(t.get("upload_id"), str) and 0 < len(t["upload_id"]) <= 64
            and t.get("method", "PUT") == "PUT"):
        raise CloudUnavailable("The cloud answered in a way this server did not understand.")
    cloud_transfer.check_url(t.get("url"))
    cloud_transfer.check_put_headers(t.get("headers"))
    return t


async def _put(target: dict, data: bytes) -> None:
    for attempt in range(3):
        try:
            await cloud_transfer.put(target["url"], target.get("headers") or {}, data)
            return
        except CloudError as e:
            status = (e.detail or {}).get("status") if isinstance(e.detail, dict) else None
            if e.code == "transfer_failed" and attempt < 2 and (status is None or status >= 500):
                await asyncio.sleep(_delay())
                continue
            raise


async def _upload_and_import(user_id: str, artifact_id: str, data: bytes, phase: _Phase, request_id: str | None) -> None:
    for _ in range(2):  # a second round only when the upload vanished before it was imported
        phase.name = "uploading"
        target = await _request_upload(user_id, len(data), request_id)
        await _put(target, data)
        phase.name = "importing"
        if await _import(user_id, artifact_id, target["upload_id"], request_id):
            return
    raise CloudError("The cloud lost the upload twice. Nothing changed here; try again.", status=502,
                     code="transfer_failed")


async def _import(user_id: str, artifact_id: str, upload_id: str, request_id: str | None) -> bool:
    """True when the cloud now has the gacabit (just made, or already there). False when the upload is
    gone and nothing was imported (upload again). Retries 503s and lost answers with the same upload id."""
    last: CloudError | None = None
    for attempt in range(settings.cloud_import_attempts):
        wait = _delay()
        try:
            await _call(user_id, "POST", "/gacabits/import", body={"upload_id": upload_id}, request_id=request_id)
            return True
        except CloudReply as r:
            code = _reply_code(r)
            if r.status == 409 and code == "conflict":  # already in this account's cloud: the earlier import worked
                if await _get_gacabit(user_id, artifact_id, request_id) is not None:
                    return True
            elif r.status == 503:
                last, wait = _map(r, "importing"), _delay(r)
            elif r.status in (404, 410) and code in ("not_found", "upload_expired"):
                # The upload is consumed or expired. Consumed because the import DID succeed? Look.
                return await _get_gacabit(user_id, artifact_id, request_id) is not None
            else:
                raise
        except CloudUnavailable as e:
            # The answer may have been lost after the cloud did the work: ask before trying again.
            last = CloudError(e.message, status=e.status, code=e.code, detail={"may_have_imported": True})
            try:
                if await _get_gacabit(user_id, artifact_id, request_id) is not None:
                    return True
            except CloudError:
                pass
        if attempt + 1 < settings.cloud_import_attempts:
            await asyncio.sleep(wait)
    assert last is not None
    raise last


async def _verify(user_id: str, artifact_id: str, bundle: dict, g: dict, request_id: str | None) -> list[str]:
    """Read the cloud's copy back and compare EVERYTHING it stores with the bundle that was sent, type-strict
    (cloud_bundle.compare_with_cloud). [] when they are identical; else the names of what differs."""
    base = f"/gacabits/{cloud_client.quote_id(artifact_id)}"
    data = _json(await _call(user_id, "GET", base + "/data", request_id=request_id))
    html = (await _call(user_id, "GET", base + "/html", request_id=request_id)).text
    versions = _json(await _call(user_id, "GET", base + "/versions", request_id=request_id))
    old_html: dict[int, str] = {}
    for v in bundle.get("html_versions") or []:
        resp = await cloud_link.call(user_id, "GET", base + "/html", params={"version": v["version"]},
                                     request_id=request_id)
        old_html[v["version"]] = resp.text
    events = _json(await cloud_link.call(user_id, "GET", base + "/events", params={"unhandled": "true", "limit": 500},
                                         request_id=request_id))
    return await asyncio.to_thread(cloud_bundle.compare_with_cloud, bundle, g, data, html, versions, old_html, events)


# ═════════════════════════ bring home ════════════════════════════════════════
async def bring_home(
    user_id: str, artifact_id: str, request_id: str | None = None, acknowledge_members: bool = False,
) -> dict[str, Any]:
    with artifact_locks.busy(artifact_id):
        if await artifacts_db.find(user_id, artifact_id) is not None:
            return await _finish_pending(user_id, artifact_id, request_id)
        pointer = await cloud_db.get_pointer(user_id, artifact_id)
        if pointer is None:
            raise NotFound("artifact", artifact_id)
        link = await cloud_link.require_link(user_id)
        phase = _Phase()
        phase.name = "starting"
        started: dict | None = None
        try:
            g0 = await _get_gacabit(user_id, artifact_id, request_id)
            if g0 is None:
                raise CloudError("The cloud has no copy of this page any more.", status=404, code="not_found")
            members = g0.get("member_count") if isinstance(g0.get("member_count"), int) else 1
            if members > 1 and not acknowledge_members:
                # Bringing a page home cuts off every member at once. Say so BEFORE doing it.
                raise CloudError(
                    f"{members - 1} other member(s) share this page and would lose access the moment it comes home. "
                    "Bring it home again with acknowledge_members set to true to go ahead. Nothing was changed.",
                    status=409, code="members_will_lose_access", detail={"member_count": members, "others": members - 1})
            started = await _start_bring_home(user_id, artifact_id, request_id)
            phase.name = "downloading"
            data = await cloud_transfer.get(_download_url(started))
            phase.name = "restoring"
            bundle = await asyncio.to_thread(cloud_bundle.unpack, data)
            prior = None
            try:
                found = await db.find_backup(artifact_id, user_id)
                prior = found["snapshot"] if found else None
            except ValueError:
                logger.warning("the sealed backup of an artifact is damaged; bringing it home without it")
            now = utcnow()
            async with artifact_service._write_lock:  # noqa: SLF001
                if await artifacts_db.find(user_id, artifact_id) is not None:
                    raise CloudError("A local page with this id appeared meanwhile.", status=409, code="conflict")
                snap = await asyncio.to_thread(
                    lambda: cloud_bundle.bundle_to_snapshot(
                        bundle, user_id=user_id, artifact_id=artifact_id, cloud_rev=int(g0.get("rev") or 0),
                        position=pointer.position, updated_at=now, prior=prior))
                await db.restore_snapshot(snap, keep_event_seq=False, bring_home={
                    "fingerprint": await asyncio.to_thread(db.fingerprint, snap), "undo_until": started.get("undo_until"),
                    "created_at": now.isoformat()})
        except asyncio.CancelledError:
            if started is not None:
                await _undo_quietly(user_id, artifact_id)
            raise
        except Exception as e:  # noqa: BLE001 - turned into a clear error below; the cloud copy is put back first
            err = _as_error(e, phase.name)
            if started is not None:
                undone = await _undo_quietly(user_id, artifact_id)
                err.detail = {**(err.detail if isinstance(err.detail, dict) else {}),
                              "cloud_state": "restored" if undone else "brought_home",
                              "undo_until": started.get("undo_until")}
                err.message += (" The cloud copy was put back as it was." if undone else
                                " The cloud copy is locked for now; bring it home again or undo it (it is kept for 7 days).")
            raise err from None
        cloud_list.forget(user_id)
        return await _finish_pending(user_id, artifact_id, request_id, undo_until=started.get("undo_until"),
                                     members_lost=max(0, members - 1))


def _as_error(e: BaseException, phase: str) -> CloudError:
    if isinstance(e, CloudReply):
        return _map(e, phase)
    if isinstance(e, cloud_bundle.BundleInvalid):
        return CloudError(f"{e} The page was not changed.", status=502, code="bundle_invalid",
                          detail={"phase": phase, **e.detail})
    if isinstance(e, CloudError):
        if not isinstance(e.detail, dict):
            e.detail = {}
        e.detail.setdefault("phase", phase)
        return e
    if isinstance(e, FileExistsError):
        return CloudError("A local page with this id appeared meanwhile.", status=409, code="conflict",
                          detail={"phase": phase})
    logger.exception("bring home failed unexpectedly (%s)", type(e).__name__)
    return CloudError("Bringing the page home failed unexpectedly.",
                      status=500, code="bring_home_failed", detail={"phase": phase})


def _download_url(started: dict) -> str:
    return cloud_transfer.check_url(started.get("download_url"))


async def _start_bring_home(user_id: str, artifact_id: str, request_id: str | None) -> dict:
    path = f"/gacabits/{cloud_client.quote_id(artifact_id)}/bring-home"
    body = {"delete_after": True, "user_confirmed": True}  # the tap in the app is the confirmation
    try:
        resp = await _call(user_id, "POST", path, body=body, request_id=request_id)
    except CloudReply as r:
        # 409 "already brought home": an earlier try flipped it and never finished. Undo that and go again.
        if not (r.status == 409 and _reply_code(r) == "conflict" and "undo_until" in str(_parse(r).get("info") or _parse(r).get("detail"))):
            raise
        await _call(user_id, "POST", path + "/undo", request_id=request_id)
        resp = await _call(user_id, "POST", path, body=body, request_id=request_id)
    started = _json(resp)
    if not isinstance(started, dict) or not isinstance(started.get("download_url"), str):
        raise CloudUnavailable("The cloud answered in a way this server did not understand.")
    return started


async def _undo_quietly(user_id: str, artifact_id: str) -> bool:
    """Best effort: put the cloud copy back as it was. True if the cloud confirmed."""
    try:
        await _call(user_id, "POST", f"/gacabits/{cloud_client.quote_id(artifact_id)}/bring-home/undo")
        return True
    except (CloudReply, CloudError):
        logger.warning("could not undo the cloud's bring-home of an artifact after a failure")
        return False


async def _finish_pending(user_id: str, artifact_id: str, request_id: str | None, undo_until: str | None = None,
                          members_lost: int = 0) -> dict:
    """Tell the cloud the page arrived (idempotent). Failing here is not a failure of the bring-home:
    the page IS home; the cloud simply keeps its copy until it hears, and we ask again later."""
    row = await db.get_bring_home(artifact_id)
    if row is None:
        raise CloudError("This page is already on this server (it did not come from the cloud).", status=409,
                         code="not_cloud_homed")
    warning = None
    confirmed = row["confirmed"]
    if not confirmed:
        try:
            await _call(user_id, "POST", f"/gacabits/{cloud_client.quote_id(artifact_id)}/bring-home/confirm",
                        request_id=request_id)
            await db.mark_bring_home_confirmed(artifact_id, undo_until)
            confirmed = True
        except CloudReply as r:
            warning = f"The page is home, but the cloud has not been told yet (it answered {r.status}). " \
                      "It will be told again later; its copy is not deleted until then."
        except CloudError as e:
            warning = f"The page is home, but the cloud could not be told yet ({e.code}). " \
                      "It will be told again later; its copy is not deleted until then."
    local = await artifact_service.get(user_id, artifact_id)
    return {"artifact": await cloud_list.local_item(user_id, local), "confirmed": confirmed,
            "undo_until": undo_until or row.get("undo_until"), "warning": warning,
            "members_lost_access": members_lost}


async def undo_bring_home(user_id: str, artifact_id: str, request_id: str | None = None) -> dict[str, Any]:
    """Cancel a bring-home inside the cloud's window. For a page still shown as a cloud page this is a
    pass-through. For one that came home, the cloud copy is restored AND the page goes back to being a cloud
    page, but only if it was not edited since it came home (else 409: nothing is overwritten)."""
    with artifact_locks.busy(artifact_id):
        link = await cloud_link.require_link(user_id)
        path = f"/gacabits/{cloud_client.quote_id(artifact_id)}/bring-home/undo"
        if await cloud_db.get_pointer(user_id, artifact_id) is not None:
            try:
                g = _json(await _call(user_id, "POST", path, request_id=request_id))
            except CloudReply as r:
                raise _map(r, "undoing") from None
            pointer = cloud_list.pointer_from(user_id, link.account_id or "", g)
            if pointer is not None:
                await cloud_db.upsert_pointer(pointer.model_copy(update={
                    "position": (await cloud_db.get_pointer(user_id, artifact_id)).position}))
            cloud_list.forget(user_id)
            return {"home": "cloud", "undone": True, "artifact": cloud_list.adapt(g if isinstance(g, dict) else {})}
        row = await db.get_bring_home(artifact_id)
        async with artifact_service._write_lock:  # noqa: SLF001
            snap = await db.read_snapshot(user_id, artifact_id)
        if row is None or snap is None:
            raise CloudError("This page was not brought home from the cloud.", status=409, code="not_brought_home")
        if db.fingerprint(snap) != row["fingerprint"]:
            raise CloudError(
                "This page was edited after it came home, so the cloud copy (which is older) will not replace it. "
                "Move the page to the cloud again instead.", status=409, code="local_changed_since_bring_home")
        try:
            g = _json(await _call(user_id, "POST", path, request_id=request_id))
        except CloudReply as r:
            raise _map(r, "undoing") from None
        try:
            await _convert(user_id, artifact_id, link, g if isinstance(g, dict) else {}, row["fingerprint"])
        except CloudError as e:
            if isinstance(e.detail, dict) or e.detail is None:
                e.detail = {**(e.detail or {}), "cloud_state": "restored"}
            raise
        cloud_list.forget(user_id)
        pointer = await cloud_db.get_pointer(user_id, artifact_id)
        return {"home": "cloud", "undone": True,
                "artifact": cloud_list.adapt(pointer.meta if pointer else {}, position=pointer.position if pointer else None)}


# ═════════════════════════ backups and housekeeping ══════════════════════════
async def restore_backup(ref: str, user_id: str | None = None) -> dict[str, Any]:
    """Put a sealed backup back as a local artifact (the CLI: `gaca-api cloud restore-backup <id>`).
    `ref` is the backup id or the artifact id. The cloud is not touched: a copy there (if any) stays,
    and anything written to it after the move is NOT in the backup."""
    try:
        found = await db.find_backup(ref, user_id)
    except ValueError as e:
        raise CloudError(str(e), status=500, code="backup_damaged") from None
    if found is None:
        raise NotFound("backup", ref)
    snap = found["snapshot"]
    artifact_id = snap["artifact"]["id"]
    async with artifact_service._write_lock:  # noqa: SLF001
        try:
            await db.restore_snapshot(snap, keep_event_seq=True)
        except FileExistsError:
            raise CloudError("A local page with this id already exists; nothing was restored.", status=409,
                             code="already_local") from None
    cloud_list.forget(found["user_id"])
    return {"artifact_id": artifact_id, "backup_id": found["id"], "title": snap["artifact"]["doc"].get("title"),
            "documents": len(snap["docs"]), "versions": len(snap["versions"]), "events": len(snap["events"]),
            "expires_at": found["expires_at"]}


_BACKUP_CHECK_EVERY = timedelta(days=1)
_PROTECT_NEWEST_DAYS = 90  # the newest backup of a page that still has a pointer is kept at least this long
_BACKUP_CEILING_DAYS = 365  # nothing is kept longer than this, verified or not
_backup_checked: dict[str, datetime] = {}


async def _backup_may_go(row: dict[str, Any], now: datetime) -> bool:
    """A backup past its 30 days is deleted only when it can no longer be needed:
    the page is local again; or the cloud copy has just been seen alive and healthy (and the newest backup of a
    page that still has a pointer is kept for 90 days regardless). A cloud copy that is missing, unreachable or
    not active keeps the backup (with a warning, rechecked daily). Hard ceiling: one year."""
    created = datetime.fromisoformat(row["created_at"])
    age = now - created
    if age >= timedelta(days=_BACKUP_CEILING_DAYS):
        logger.warning("deleting a cloud backup after %d days, unverified", _BACKUP_CEILING_DAYS)
        return True
    artifact_id, user_id = row["artifact_id"], row["user_id"]
    if await artifacts_db.find(user_id, artifact_id) is not None:
        return True  # the page lives here again: its content is not at risk
    pointer = await cloud_db.get_pointer(user_id, artifact_id)
    if pointer is None:
        logger.warning("keeping the cloud backup of page %s: it has no pointer to check the cloud copy with", artifact_id)
        return False
    if age < timedelta(days=_PROTECT_NEWEST_DAYS) and await db.newest_backup_id(artifact_id) == row["id"]:
        return False
    last = _backup_checked.get(row["id"])
    if last is not None and now - last < _BACKUP_CHECK_EVERY:
        return False
    _backup_checked[row["id"]] = now
    try:
        g = await _get_gacabit(user_id, artifact_id)
    except (CloudError, CloudReply):
        logger.warning("keeping the cloud backup of page %s: the cloud could not be asked; checking again tomorrow",
                       artifact_id)
        return False
    if g is None or g.get("status") != "active":
        logger.warning("keeping the cloud backup of page %s: the cloud copy is %s; checking again tomorrow",
                       artifact_id, "missing" if g is None else g.get("status"))
        return False
    return True


async def maintenance(now: datetime | None = None) -> dict[str, int]:
    """Housekeeping, safe to run any time: delete backups that are past their expiry AND whose cloud copy was just
    verified healthy (see `_backup_may_go`), and tell the cloud about pages that came home but whose confirm never
    got through."""
    global _last_maintenance
    now = now or utcnow()
    _last_maintenance = now
    removed = kept = 0
    for row in await db.due_backups(now):
        if await _backup_may_go(row, now):
            removed += await db.delete_backup(row["id"])
            _backup_checked.pop(row["id"], None)
        else:
            kept += 1
    confirmed = 0
    for row in await db.list_bring_homes():
        if row["confirmed"]:
            continue
        try:
            # Not while a bring-home / undo of this page is running: a confirm sent in that window can start the
            # cloud's 7-day purge clock early.
            with artifact_locks.busy(row["artifact_id"]):
                await _call(row["user_id"], "POST",
                            f"/gacabits/{cloud_client.quote_id(row['artifact_id'])}/bring-home/confirm")
                await db.mark_bring_home_confirmed(row["artifact_id"])
            confirmed += 1
        except (CloudReply, CloudError):
            pass  # not linked / cloud down / busy: try again next time
    return {"backups_removed": removed, "backups_kept": kept, "confirms_sent": confirmed}


async def maintenance_if_due(now: datetime | None = None) -> None:
    now = now or utcnow()
    if _last_maintenance is None or now - _last_maintenance >= _MAINTENANCE_EVERY:
        try:
            await maintenance(now)
        except Exception:  # noqa: BLE001 - housekeeping must never take the server down
            logger.exception("cloud maintenance failed")


async def _maintenance_loop() -> None:
    await asyncio.sleep(60)  # not while the server is still starting
    while True:
        await maintenance_if_due()
        await asyncio.sleep(600)


def start_maintenance() -> asyncio.Task:
    return asyncio.create_task(_maintenance_loop(), name="cloud-maintenance")


async def stop_maintenance(task: asyncio.Task | None) -> None:
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
