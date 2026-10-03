#!/usr/bin/env python3
"""Helper for the david-email-calendar-suggestions skill. Standard library only.

  fetch [--days N] [--max N]   new inbox mail (read-only) + what was already suggested, as JSON
  finish [--file PATH]         read the agent's suggestions JSON (file, else stdin), post ONE GACA alert

Email is only ever READ (Gmail search/get through the google-workspace skill's
script); nothing is sent, labeled, moved or deleted. State lives in
~/.hermes/cron/email-calendar-suggestions-state.json so a message is read once and
an event is suggested once. Tokens come from the environment, never printed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/New_York")
HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
GACA_BASE = os.environ.get("GACA_BASE_URL", "https://goodandcheapagent.com").rstrip("/")
GOOGLE_API = HOME / "skills/productivity/google-workspace/scripts/google_api.py"
# The system python lacks the Google libraries; Hermes' own virtualenv has them.
VENV_PY = HOME / "hermes-agent/venv/bin/python"
STATE = HOME / "cron/email-calendar-suggestions-state.json"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131 Safari/537.36 Hermes-Email-Calendar/1.0"

# School/team mail lands in Primary or Updates; skip the obviously-not-family tabs.
BASE_QUERY = "in:inbox -category:promotions -category:social -in:spam -in:trash"
CALENDAR_BASE = "https://calendar.rabidflamingos.com"
BODY_LIMIT = 3500
KEEP_DAYS = 30
MAX_SUGGESTIONS = 10


def die(message: str, code: int = 1):
    print(message, file=sys.stderr)
    raise SystemExit(code)


# ── State ────────────────────────────────────────────────────────────────────
def load_state() -> dict:
    try:
        value = json.loads(STATE.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as error:
        die(f"Cannot read state {STATE}: {error}")


def save_state(state: dict) -> None:
    cutoff = (date.today() - timedelta(days=KEEP_DAYS)).isoformat()
    state["processed"] = {k: v for k, v in state.get("processed", {}).items() if v >= cutoff}
    state["suggested"] = {k: v for k, v in state.get("suggested", {}).items() if v.get("on", "") >= cutoff}
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    tmp.replace(STATE)


def fingerprint(suggestion: dict) -> str:
    """Same event = same title (loosely) on the same day, however the email words it."""
    title = re.sub(r"[^a-z0-9]+", " ", str(suggestion.get("title", "")).lower()).strip()
    return hashlib.sha256(f"{suggestion.get('date')}|{title}".encode()).hexdigest()[:12]


# ── Gmail (read-only) ────────────────────────────────────────────────────────
def gmail(*args: str):
    python = str(VENV_PY) if VENV_PY.exists() else sys.executable
    result = subprocess.run([python, str(GOOGLE_API), "gmail", *args], capture_output=True, text=True, timeout=90)
    if result.returncode != 0:
        die(f"Gmail call failed: {(result.stderr or result.stdout).strip()[:300]}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        die("Gmail returned something that isn't JSON")


def cmd_fetch(args) -> None:
    state = load_state()
    seen = state.get("processed", {})
    found = gmail("search", f"{BASE_QUERY} newer_than:{args.days}d", "--max", str(args.max))
    emails = []
    for item in found:
        if item["id"] in seen:
            continue
        full = gmail("get", item["id"])
        body = re.sub(r"\n{3,}", "\n\n", (full.get("body") or item.get("snippet") or "").strip())
        emails.append({
            "id": item["id"],
            "thread_id": item.get("threadId", ""),
            "from": item.get("from", ""),
            "subject": item.get("subject", ""),
            "date": item.get("date", ""),
            "body": body[:BODY_LIMIT],
        })
    print(json.dumps({
        "today": datetime.now(TZ).date().isoformat(),
        "emails": emails,
        "recently_suggested": [
            {"date": v["date"], "title": v["title"]} for v in state.get("suggested", {}).values()
        ],
    }, indent=1))


# ── GACA ─────────────────────────────────────────────────────────────────────
def gaca_request(method: str, path: str, token: str, payload: object | None = None):
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT, "Authorization": f"Bearer {token}"}
    body = None
    if payload is not None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        headers["Content-Type"] = "application/json"
    for attempt in range(3):
        req = urllib.request.Request(GACA_BASE + path, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            if error.code < 500 or attempt == 2:
                die(f"{method} {path} returned HTTP {error.code}: {error.read().decode('utf-8', 'replace')[:300]}")
        except urllib.error.URLError as error:
            if attempt == 2:
                die(f"Network error calling GACA: {error.reason}")
        time.sleep(2**attempt)


def when_text(s: dict) -> str:
    try:
        day = date.fromisoformat(s["date"]).strftime("%a %b %d").replace(" 0", " ")
    except (KeyError, ValueError):
        day = str(s.get("date", "?"))
    return f"{day}, {s['time']}" if s.get("time") else day


def week_start(day: date) -> date:
    """The Monday of `day`'s week — the calendar app's pages are keyed by it."""
    return day - timedelta(days=day.weekday())


def build_links(suggestions: list[dict]) -> list[dict]:
    """GACA alert links for a batch: the calendar week of the earliest suggestion, then one per
    distinct source email (opens that Gmail thread). Gmail's `u/0` is the first signed-in account
    on the phone — the one this inbox belongs to. Thread ids are checked to be hex so nothing else
    can end up inside the URL."""
    links: list[dict] = []
    if suggestions:
        first = min(date.fromisoformat(s["date"]) for s in suggestions)
        links.append({
            "label": "Open family calendar",
            "url": f"{CALENDAR_BASE}/#week-card-{week_start(first).isoformat()}",
        })
    seen: set[str] = set()
    for s in suggestions:
        thread = str(s.get("source_thread_id") or "").strip()
        if not re.fullmatch(r"[0-9a-fA-F]{8,40}", thread) or thread in seen:
            continue
        seen.add(thread)
        subject = " ".join(str(s.get("source_subject") or "").split()) or "email"
        label = f"Email: {subject}"
        links.append({
            "label": label if len(label) <= 80 else label[:79].rstrip() + "…",
            "url": f"https://mail.google.com/mail/u/0/#all/{thread}",
        })
    return links[:10]


def line_for(s: dict) -> str:
    text = f"• {when_text(s)} — {s['title']}"
    if s.get("who"):
        text += f" ({s['who']})"
    if s.get("location"):
        text += f" at {s['location']}"
    return text


def cmd_finish(args) -> None:
    token = os.environ.get("GACA_API_TOKEN", "").strip()
    if not token:
        die("GACA_API_TOKEN is not configured")
    if args.file:
        path = Path(args.file).expanduser()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            die(f"Cannot read {path}: {error}")
        path.unlink(missing_ok=True)  # one-shot hand-off; don't leave it lying around
    else:
        payload = json.load(sys.stdin)
    if not isinstance(payload, dict):
        die("Expected a JSON object with 'suggestions' and 'processed_ids'")
    state = load_state()
    today = datetime.now(TZ).date()

    # Keep only well-formed, future, not-yet-suggested events.
    fresh: list[dict] = []
    known = set(state.get("suggested", {}))
    for s in payload.get("suggestions") or []:
        if not isinstance(s, dict) or not s.get("title"):
            continue
        try:
            when = date.fromisoformat(str(s.get("date")))
        except ValueError:
            continue
        fp = fingerprint(s)
        if when < today or fp in known:
            continue
        known.add(fp)
        s["_fp"] = fp
        fresh.append(s)
    fresh = fresh[:MAX_SUGGESTIONS]
    fresh.sort(key=lambda s: (s["date"], s.get("time") or ""))

    # Emails are marked read only now that we've got this far; a crash earlier just
    # means they're looked at again tomorrow.
    for message_id in payload.get("processed_ids") or []:
        state.setdefault("processed", {})[str(message_id)] = today.isoformat()

    if not fresh:
        save_state(state)
        print("No new calendar suggestions; no alert sent.")
        return

    lines = [line_for(s) for s in fresh]
    details = []
    for s in fresh:
        why = " — ".join(x for x in (s.get("source_subject"), s.get("source_from")) if x)
        details.append(f"{line_for(s)}\n   from: {why}" if why else line_for(s))
    n = len(fresh)
    alert = {
        "source": "email-calendar-suggestions",
        "kind": "calendar.suggestions",
        "severity": "info",
        "title": f"{n} thing{'s' if n != 1 else ''} from your email to add to the calendar",
        "body": "\n".join(details),
        "data": {"date": today.isoformat(), "suggestions": [{k: v for k, v in s.items() if k != "_fp"} for s in fresh]},
        "dedupe_key": f"calendar:suggestions:{today.isoformat()}",
        "links": build_links(fresh),
        # No approve button yet — David takes it to chat and asks Hermes to add them.
        "suggested_actions": [],
    }
    created = gaca_request("POST", "/api/alerts", token, alert)
    if not isinstance(created, dict) or not created.get("id"):
        die("GACA did not return a stored alert id")

    for s in fresh:
        state.setdefault("suggested", {})[s["_fp"]] = {"date": s["date"], "title": s["title"], "on": today.isoformat()}
    save_state(state)
    print(f"alert {created['id']}: {n} suggestion(s)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("fetch"); p.add_argument("--days", type=int, default=2); p.add_argument("--max", type=int, default=40)
    p.set_defaults(fn=cmd_fetch)
    p = sub.add_parser("finish"); p.add_argument("--file"); p.set_defaults(fn=cmd_finish)
    args = parser.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
