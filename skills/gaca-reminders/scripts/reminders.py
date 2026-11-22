#!/usr/bin/env python3
"""Helper for the gaca-reminders skill. Standard library only.

A reminder is something the user asked to be nudged about (or accepted when you offered). GACA keeps
it and, when it is due, raises an ordinary alert on their phone; you do not have to remember it or
schedule anything. A reminder with no time is a "later" note: it never pings, it just waits.

  now                           the current date, time and time zone for the user (use this, not a guess)
  create --text T [--at TIME] [--explicit] [--kind K] [--session ID] [--alert-id ID] [--expires TIME]
                                make a reminder (or, with no --at, a later note). TIME is ISO 8601,
                                e.g. 2026-11-23T10:00 (the user's time zone is assumed) or ...T10:00-05:00 or ...Z.
                                --explicit only when the USER named the time ("at 10"), not when you chose it.
  list [--state S]              pending (default), fired, done, cancelled, expired, or open
  reschedule ID --at TIME [--explicit]
  cancel ID                     drop a pending reminder (the user's "undo")
  done ID                       it is dealt with

Secrets come from the environment (GACA_API_TOKEN) and are never printed. The user's time zone comes
from the GACA app's settings, else GACA_TIMEZONE (e.g. America/Chicago), else the machine's own.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

GACA_BASE = os.environ.get("GACA_BASE_URL", "http://127.0.0.1:41700").rstrip("/")
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131 Safari/537.36 Hermes-GACA-Reminders/1.0"
# The heartbeat is what delivers reminders and it wakes every half hour, so say so rather than promise a minute.
DELIVERY_NOTE = "delivered within half an hour of that time"


def die(message: str, code: int = 1):
    print(message, file=sys.stderr)
    raise SystemExit(code)


def token() -> str:
    value = os.environ.get("GACA_API_TOKEN", "").strip()
    if not value:
        die("GACA_API_TOKEN is not configured")
    if "\n" in value or "\r" in value:
        die("GACA_API_TOKEN contains an invalid newline")
    return value


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def request(method: str, path: str, payload: object | None = None, *, tolerant: bool = False):
    """JSON request to the local GACA API, retrying 5xx and network errors. `tolerant`: return None
    instead of stopping on any failure (for optional lookups)."""
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT, "Authorization": f"Bearer {token()}"}
    data = None
    if payload is not None:
        data = json.dumps(payload, separators=(",", ":")).encode()
        headers["Content-Type"] = "application/json"
    opener = urllib.request.build_opener(NoRedirects)
    last = "no answer"
    for attempt in range(1 if tolerant else 3):
        req = urllib.request.Request(f"{GACA_BASE}{path}", data=data, headers=headers, method=method)
        try:
            with opener.open(req, timeout=15 if tolerant else 30) as response:
                raw = response.read().decode("utf-8", errors="replace")
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")[:500]
            if error.code < 500:
                if tolerant:
                    return None
                die(f"GACA answered {error.code}: {body}", 2)
            last = f"{error.code}: {body}"
        except (urllib.error.URLError, OSError, ValueError) as error:
            last = str(getattr(error, "reason", error))
        if not tolerant:
            time.sleep(1 + attempt)
    if tolerant:
        return None
    die(f"GACA did not answer ({last})", 2)


def zone():
    """The user's time zone: the app's setting, else GACA_TIMEZONE, else the machine's own."""
    settings = request("GET", "/api/heartbeat/settings", tolerant=True)
    name = ((settings or {}).get("timezone") or os.environ.get("GACA_TIMEZONE", "")).strip()
    if name:
        try:
            return ZoneInfo(name)
        except Exception:  # noqa: BLE001
            die(f"time zone {name!r} is not a time zone name (try America/Chicago)")
    return datetime.now().astimezone().tzinfo


def now() -> datetime:
    stamp = os.environ.get("GACA_REMINDERS_NOW")  # tests only
    base = datetime.fromisoformat(stamp) if stamp else datetime.now(timezone.utc)
    return base.astimezone(zone())


def parse_time(text: str, label: str = "--at") -> str:
    """TIME as a UTC ISO string. No offset means the user's own zone."""
    try:
        moment = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        die(f"{label} {text!r} is not a time; use ISO 8601, for example 2026-11-23T10:00")
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=zone())
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def when_local(iso: str | None) -> str | None:
    if not iso:
        return None
    moment = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(zone())
    today = now().date()
    clock = moment.strftime("%-I:%M %p")
    if moment.date() == today:
        return f"today at {clock}"
    return f"{moment:%A %b} {moment.day} at {clock}"


def summary(reminder: dict) -> dict:
    return {
        "id": reminder["id"], "text": reminder["text"], "kind": reminder["kind"], "state": reminder["state"],
        "when": when_local(reminder.get("due_at")), "due_at": reminder.get("due_at"),
        "explicit_time": reminder.get("explicit_time"),
    }


def cmd_now(_args) -> None:
    moment = now()
    print(json.dumps({"now": moment.isoformat(timespec="minutes"), "weekday": moment.strftime("%A"),
                      "zone": str(moment.tzinfo), "utc_offset": moment.strftime("%z")}))


def cmd_create(args) -> None:
    due = parse_time(args.at) if args.at else None
    if args.explicit and not due:
        die("--explicit needs --at")
    source = {"kind": args.kind, "crew": os.environ.get("HERMES_PROFILE", "").strip() or "default"}
    if args.session:
        source["session_id"] = args.session
    if args.alert_id:
        source["alert_id"] = args.alert_id
    body: dict = {"text": args.text, "source": source}
    if due:
        body["due_at"], body["explicit_time"] = due, bool(args.explicit)
    if args.expires:
        body["expires_at"] = parse_time(args.expires, "--expires")
    made = request("POST", "/api/reminders", body)
    out = summary(made)
    out["duplicate"] = bool(made.get("duplicate"))
    if due:
        out["confirm"] = f"Reminder set for {out['when']} ({DELIVERY_NOTE})."
    else:
        out["confirm"] = "Noted for later. It will not ping you."
    if out["duplicate"]:
        out["confirm"] = "You already had that one: " + out["confirm"]
    print(json.dumps(out, indent=2))


def cmd_list(args) -> None:
    query = f"?state={args.state}" if args.state else ""
    body = request("GET", f"/api/reminders{query}") or {}
    print(json.dumps([summary(r) for r in body.get("reminders", [])], indent=2))


def cmd_reschedule(args) -> None:
    body = {"due_at": parse_time(args.at), "explicit_time": bool(args.explicit)}
    print(json.dumps(summary(request("PATCH", f"/api/reminders/{args.id}", body)), indent=2))


def cmd_end(state: str):
    def run(args) -> None:
        print(json.dumps(summary(request("PATCH", f"/api/reminders/{args.id}", {"state": state})), indent=2))
    return run


def main() -> int:
    parser = argparse.ArgumentParser(description="Make, list and change the user's reminders in GACA")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("now", help="the current date, time and zone")
    create = sub.add_parser("create", help="make a reminder, or a later note")
    create.add_argument("--text", required=True)
    create.add_argument("--at", help="ISO 8601 time; leave out for a later note")
    create.add_argument("--explicit", action="store_true", help="the user named the time")
    create.add_argument("--kind", choices=["chat", "alert", "agent"], default="agent")
    create.add_argument("--session")
    create.add_argument("--alert-id")
    create.add_argument("--expires", help="notes only: when to drop it (default 30 days)")
    listing = sub.add_parser("list", help="show reminders")
    listing.add_argument("--state", choices=["pending", "fired", "done", "cancelled", "expired", "open"])
    resched = sub.add_parser("reschedule", help="move a reminder to a new time")
    resched.add_argument("id")
    resched.add_argument("--at", required=True)
    resched.add_argument("--explicit", action="store_true")
    for name, helptext in (("cancel", "drop a pending reminder"), ("done", "mark one dealt with")):
        one = sub.add_parser(name, help=helptext)
        one.add_argument("id")
    args = parser.parse_args()
    {"now": cmd_now, "create": cmd_create, "list": cmd_list, "reschedule": cmd_reschedule,
     "cancel": cmd_end("cancelled"), "done": cmd_end("done")}[args.command](args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
