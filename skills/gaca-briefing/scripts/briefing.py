#!/usr/bin/env python3
"""Helper for the gaca-briefing skill. Standard library only.

A briefing is one day's digest made of independent sections (calendar, weather, news, tasks,
whatever the user wants). This helper only talks to GACA; deciding what goes in each section is
the agent's job.

  today                          print today's date (the user's time zone)
  show --date D                  print the day's briefing as GACA has it
  post --date D --key K [--file PATH]
                                 PUT one section (JSON from PATH or stdin). An empty section
                                 (no items and no headline) is removed instead, never posted blank.
  delete --date D --key K        remove a section
  summary --date D [--text T]    set the day's 1-3 sentence summary (T, or stdin)
  finish --date D [--problem T ...]
                                 send the "briefing ready" alert (a warning if there were problems)

Secrets come from the environment (GACA_API_TOKEN) and are never printed. Dates are YYYY-MM-DD in the
user's own time zone: set GACA_TIMEZONE (e.g. America/Chicago) or the machine's local zone is used.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# GACA runs on the same machine as Hermes; always talk to it locally, never via a public URL.
GACA_BASE = os.environ.get("GACA_BASE_URL", "http://127.0.0.1:41700").rstrip("/")
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131 Safari/537.36 Hermes-GACA-Briefing/1.0"
KEY = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
KINDS = ("general", "calendar", "weather", "news", "sports", "tasks")


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


def request(method: str, path: str, payload: object | None = None):
    """JSON request to the local GACA API, retrying 5xx/network errors. Returns the decoded body."""
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT, "Authorization": f"Bearer {token()}"}
    body = None
    if payload is not None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    for attempt in range(3):
        req = urllib.request.Request(GACA_BASE + path, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:500]
            if error.code < 500 or attempt == 2:
                die(f"{method} {path} returned HTTP {error.code}: {detail}")
        except urllib.error.URLError as error:
            if attempt == 2:
                die(f"Could not reach GACA at {GACA_BASE}: {error.reason}")
        time.sleep(2**attempt)
    die("unreachable")


def today() -> date:
    zone = os.environ.get("GACA_TIMEZONE", "").strip()
    if not zone:
        return datetime.now().astimezone().date()  # the machine's own local zone
    try:
        return datetime.now(ZoneInfo(zone)).date()
    except Exception:
        die(f"GACA_TIMEZONE {zone!r} is not a valid time zone name (e.g. America/Chicago)")


def day_arg(value: str | None) -> date:
    if not value:
        return today()
    try:
        return date.fromisoformat(value)
    except ValueError:
        die(f"Bad date {value!r}; expected YYYY-MM-DD")


def key_arg(value: str) -> str:
    if not KEY.fullmatch(value):
        die(f"Bad section key {value!r}: lowercase letters, digits, - and _ (e.g. weather, news, tasks)")
    return value


def cmd_show(args) -> None:
    print(json.dumps(request("GET", f"/api/briefings?date={day_arg(args.date).isoformat()}"), indent=2))


def cmd_post(args) -> None:
    day, key = day_arg(args.date), key_arg(args.key)
    raw = Path(args.file).read_text() if args.file else sys.stdin.read()
    try:
        section = json.loads(raw)
    except json.JSONDecodeError as error:
        die(f"Section JSON is not valid: {error}")
    if not isinstance(section, dict):
        die("Section JSON must be an object")
    if section.get("kind", "general") not in KINDS:
        die(f"kind must be one of {', '.join(KINDS)}")
    path = f"/api/briefings/{day.isoformat()}/sections/{key}"
    if not section.get("items") and not section.get("headline"):
        try:
            request("DELETE", path)
            print(f"{key}: empty, removed")
        except SystemExit:
            print(f"{key}: empty, nothing to remove")
        return
    out = request("PUT", path, section)
    print(f"{key}: posted ({len(out.get('items', []))} items)")


def cmd_delete(args) -> None:
    request("DELETE", f"/api/briefings/{day_arg(args.date).isoformat()}/sections/{key_arg(args.key)}")
    print(f"{args.key}: removed")


def cmd_summary(args) -> None:
    text = (args.text if args.text is not None else sys.stdin.read()).strip()
    request("PUT", f"/api/briefings/{day_arg(args.date).isoformat()}", {"summary": text or None})
    print("summary: posted")


def profile_name() -> str:
    """The Hermes profile this runs as ("default", "research"...): the `crew` GACA attributes the alert
    to, so taking it to chat opens that crew member. Mirrors the gaca-alerts helper."""
    explicit = os.environ.get("HERMES_PROFILE", "").strip()
    if explicit:
        return explicit
    home = Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser().resolve()
    default = (Path.home() / ".hermes").resolve()
    if home.parent == default / "profiles" and home.name:
        return home.name
    return "default"


def cmd_finish(args) -> None:
    day = day_arg(args.date)
    briefing = request("GET", f"/api/briefings?date={day.isoformat()}")
    sections = briefing.get("sections", [])
    if not sections and not briefing.get("summary"):
        die("Briefing is empty; not sending a 'ready' alert", 2)
    parts = [f"{s['title']} ({len(s.get('items', []))})" if s.get("items") else s["title"] for s in sections]
    body = "Posted: " + "; ".join(parts) + "."
    if args.problem:
        body += "\nProblems: " + "; ".join(args.problem)
    payload = {
        "source": "daily-briefing",
        "crew": profile_name(),
        "kind": "briefing.posted",
        "severity": "warn" if args.problem else "info",
        "title": "Daily briefing ready (with gaps)" if args.problem else "Daily briefing ready",
        "body": body,
        "data": {"date": day.isoformat(), "sections": [s["key"] for s in sections]},
        "dedupe_key": f"briefing:posted:{day.isoformat()}",
        "suggested_actions": [],
    }
    created = request("POST", "/api/alerts", payload)
    print(f"alert: {created.get('id', '?') if isinstance(created, dict) else '?'}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("today").set_defaults(fn=lambda a: print(today().isoformat()))
    p = sub.add_parser("show"); p.add_argument("--date"); p.set_defaults(fn=cmd_show)
    p = sub.add_parser("post"); p.add_argument("--date", required=True); p.add_argument("--key", required=True)
    p.add_argument("--file"); p.set_defaults(fn=cmd_post)
    p = sub.add_parser("delete"); p.add_argument("--date", required=True); p.add_argument("--key", required=True)
    p.set_defaults(fn=cmd_delete)
    p = sub.add_parser("summary"); p.add_argument("--date", required=True); p.add_argument("--text")
    p.set_defaults(fn=cmd_summary)
    p = sub.add_parser("finish"); p.add_argument("--date", required=True)
    p.add_argument("--problem", action="append", default=[]); p.set_defaults(fn=cmd_finish)
    args = parser.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
