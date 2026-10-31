#!/usr/bin/env python3
"""Helper for the gaca-heartbeat skill. Standard library only.

The heartbeat is a short, frequent check-in: look at what you can see, and tell the user only about
what is worth interrupting them for. This helper owns the parts that must not depend on the model's
mood: whether to run at all (quiet hours, daily budget, back-off), the daily alert cap, and the
memory of what the user did with earlier alerts. Deciding what is worth saying is the agent's job.

  gate                          should this run happen? prints {"run": bool, "reason": ...}.
                                Counts the run. If "run" is false, stop and reply [SILENT].
  context [--days N]            what you already told the user lately and what they did with it
                                (acted / dismissed / snoozed / still new), plus today's budget.
  alert                         post one alert (JSON on stdin) through the caps. Refuses (exit 3)
                                when the cap or a gap says to hold it, or the user already dismissed it.
  done --outcome O [--note T]   record how the run ended: quiet | alerted | error | rate_limited.
                                rate_limited pauses the heartbeat so it cannot burn the plan further.
  config [--set key=value ...]  show (or change) the budget settings.

Secrets come from the environment (GACA_API_TOKEN) and are never printed. Times are the user's own
time zone: set GACA_TIMEZONE (e.g. America/Chicago) or the machine's local zone is used.
State lives in $HERMES_HOME/gaca-heartbeat/ (state.json, config.json), one per Hermes profile.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

GACA_BASE = os.environ.get("GACA_BASE_URL", "http://127.0.0.1:41700").rstrip("/")
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131 Safari/537.36 Hermes-GACA-Heartbeat/1.0"
SOURCE = "heartbeat"
KIND_PREFIX = "heartbeat."

# Every limit can be changed with `config --set`; these are the starting point. A 30-minute
# heartbeat between 07:00 and 22:00 is 30 runs, so 30 is "every run in the waking day".
DEFAULTS = {
    "quiet_start_hour": 22,        # no runs from this hour...
    "quiet_end_hour": 7,           # ...until this one (local time)
    "max_runs_per_day": 30,
    "max_alerts_per_day": 3,
    "min_minutes_between_alerts": 60,
    "rate_limit_pause_hours": 3,   # how long `done --outcome rate_limited` pauses everything
    "remember_days": 14,           # how far back `context` looks
}
INTS = set(DEFAULTS)


def die(message: str, code: int = 1):
    print(message, file=sys.stderr)
    raise SystemExit(code)


# ── time and state ──────────────────────────────────────────────────

def zone() -> ZoneInfo | None:
    name = os.environ.get("GACA_TIMEZONE", "").strip()
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - a bad zone name must not stop the heartbeat
        die(f"GACA_TIMEZONE {name!r} is not a time zone name (try America/Chicago)")


def now() -> datetime:
    """The current time in the user's zone, as an aware datetime."""
    stamp = os.environ.get("GACA_HEARTBEAT_NOW")  # tests only
    if stamp:
        return datetime.fromisoformat(stamp)
    tz = zone()
    return datetime.now(tz) if tz else datetime.now().astimezone()


def state_dir() -> Path:
    home = Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()
    path = home / "gaca-heartbeat"
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_json(path: Path, fallback: dict) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else fallback
    except (OSError, json.JSONDecodeError):
        return fallback


def write_json(path: Path, data: dict) -> None:
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=2, sort_keys=True))
    temp.replace(path)


def load_config() -> dict:
    config = dict(DEFAULTS)
    for key, value in read_json(state_dir() / "config.json", {}).items():
        if key in DEFAULTS and isinstance(value, int) and not isinstance(value, bool):
            config[key] = value
    return config


def load_state() -> dict:
    state = read_json(state_dir() / "state.json", {})
    today = now().date().isoformat()
    if state.get("day") != today:  # a new day starts a fresh count; the pause survives
        state = {"day": today, "runs": 0, "alerts": 0, "last_alert_at": state.get("last_alert_at"),
                 "paused_until": state.get("paused_until"), "last_outcome": state.get("last_outcome")}
    return state


def save_state(state: dict) -> None:
    write_json(state_dir() / "state.json", state)


def in_quiet_hours(moment: datetime, config: dict) -> bool:
    start, end = config["quiet_start_hour"], config["quiet_end_hour"]
    hour = moment.hour
    if start == end:
        return False
    return hour >= start or hour < end if start > end else start <= hour < end


# ── GACA ────────────────────────────────────────────────────────────

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


def request(method: str, path: str, payload: object | None = None):
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT, "Authorization": f"Bearer {token()}"}
    data = None
    if payload is not None:
        data = json.dumps(payload, separators=(",", ":")).encode()
        headers["Content-Type"] = "application/json"
    opener = urllib.request.build_opener(NoRedirects)
    last = "no answer"
    for attempt in range(3):
        req = urllib.request.Request(f"{GACA_BASE}{path}", data=data, headers=headers, method=method)
        try:
            with opener.open(req, timeout=30) as response:
                raw = response.read().decode("utf-8", errors="replace")
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")[:500]
            if error.code < 500:
                die(f"GACA answered {error.code}: {body}", 2)
            last = f"{error.code}: {body}"
        except urllib.error.URLError as error:
            last = str(error.reason)
        time.sleep(1 + attempt)
    die(f"GACA did not answer ({last})", 2)


def recent_alerts(days: int) -> list[dict]:
    """This heartbeat's own alerts from the last `days` days, newest first."""
    body = request("GET", "/api/alerts?limit=200") or {}
    cutoff = now() - timedelta(days=days)
    mine = []
    for alert in body.get("alerts", []):
        if alert.get("source") != SOURCE:
            continue
        try:
            created = datetime.fromisoformat(str(alert["created_at"]).replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=cutoff.tzinfo)
        if created >= cutoff:
            mine.append(alert)
    return mine


def status_of(alert: dict) -> str:
    return str(alert.get("effective_status") or alert.get("status") or "new")


# ── commands ────────────────────────────────────────────────────────

def cmd_gate(_args) -> int:
    config, state, moment = load_config(), load_state(), now()
    reason = None
    paused = state.get("paused_until")
    if paused and datetime.fromisoformat(paused) > moment:
        reason = f"paused until {paused} (the model provider said it was out of budget)"
    elif in_quiet_hours(moment, config):
        reason = f"quiet hours ({config['quiet_start_hour']}:00-{config['quiet_end_hour']}:00)"
    elif state["runs"] >= config["max_runs_per_day"]:
        reason = f"daily run budget used ({state['runs']}/{config['max_runs_per_day']})"
    if reason is None:
        state["runs"] += 1
        save_state(state)
    print(json.dumps({"run": reason is None, "reason": reason or "ok",
                      "runs_today": state["runs"], "now": moment.isoformat(timespec="minutes")}))
    return 0


def cmd_context(args) -> int:
    config, state = load_config(), load_state()
    days = args.days or config["remember_days"]
    alerts = recent_alerts(days)
    left = max(0, config["max_alerts_per_day"] - state["alerts"])
    print(f"Now: {now().isoformat(timespec='minutes')}")
    print(f"Alert budget today: {left} of {config['max_alerts_per_day']} left; "
          f"at least {config['min_minutes_between_alerts']} minutes between alerts.")
    if state.get("last_alert_at"):
        print(f"Last alert sent: {state['last_alert_at']}")
    print(f"\nWhat you told the user in the last {days} days, and what they did with it:")
    if not alerts:
        print("  (nothing yet: you have no record of what this user cares about)")
        return 0
    by_kind: dict[str, dict[str, int]] = {}
    for alert in alerts:
        counts = by_kind.setdefault(alert.get("kind", "?"), {})
        counts[status_of(alert)] = counts.get(status_of(alert), 0) + 1
    for alert in alerts[:40]:
        print(f"  [{status_of(alert):9}] {str(alert.get('created_at', ''))[:16]}  {alert.get('kind')}  "
              f"{alert.get('title')}  (key: {alert.get('dedupe_key') or '-'})")
    print("\nBy kind (acted = they did something with it; dismissed = it was not worth their time):")
    for kind, counts in sorted(by_kind.items()):
        print(f"  {kind}: " + ", ".join(f"{n} {status}" for status, n in sorted(counts.items())))
    return 0


def cmd_alert(_args) -> int:
    if sys.stdin.isatty():
        die("Read the alert JSON from standard input")
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as error:
        die(f"Invalid JSON on standard input: {error}")
    if not isinstance(payload, dict):
        die("Alert JSON must be an object")
    config, state, moment = load_config(), load_state(), now()

    kind = str(payload.get("kind") or "").strip()
    if not kind:
        die("The alert needs a kind")
    if not kind.startswith(KIND_PREFIX):
        kind = KIND_PREFIX + kind
    payload["kind"], payload["source"] = kind, SOURCE
    payload.setdefault("crew", os.environ.get("HERMES_PROFILE", "").strip() or "default")
    if payload.get("severity") not in (None, "info", "warn", "urgent"):
        die("severity must be info, warn or urgent")
    key = payload.get("dedupe_key")

    existing = None
    if key:
        existing = next((a for a in recent_alerts(config["remember_days"]) if a.get("dedupe_key") == key), None)
    if existing and status_of(existing) in ("dismissed", "acted", "completed"):
        print(f"HOLD: the user already {status_of(existing)} an alert with key {key!r}. Do not raise it again "
              "unless something materially new happened, and then use a new key.", file=sys.stderr)
        return 3

    if not existing:  # re-posting the same open alert updates it and costs no budget
        if state["alerts"] >= config["max_alerts_per_day"] and payload.get("severity") != "urgent":
            print(f"HOLD: {state['alerts']}/{config['max_alerts_per_day']} alerts already sent today. "
                  "Keep this for a later run or mention it in tomorrow's check-in.", file=sys.stderr)
            return 3
        last = state.get("last_alert_at")
        if last and payload.get("severity") != "urgent":
            gap = (moment - datetime.fromisoformat(last)).total_seconds() / 60
            if gap < config["min_minutes_between_alerts"]:
                print(f"HOLD: the last alert was {int(gap)} minutes ago (minimum "
                      f"{config['min_minutes_between_alerts']}). Fold this into the next one.", file=sys.stderr)
                return 3

    body = request("POST", "/api/alerts", payload)
    if not existing:
        state["alerts"] += 1
        state["last_alert_at"] = moment.isoformat(timespec="seconds")
    state["last_outcome"] = "alerted"
    save_state(state)
    print(json.dumps({"posted": True, "id": (body or {}).get("id"), "alerts_today": state["alerts"]}))
    return 0


def cmd_done(args) -> int:
    config, state = load_config(), load_state()
    state["last_outcome"] = args.outcome
    if args.outcome == "rate_limited":
        until = now() + timedelta(hours=config["rate_limit_pause_hours"])
        state["paused_until"] = until.isoformat(timespec="minutes")
        print(f"Paused until {state['paused_until']}.")
    save_state(state)
    if args.note:
        print(f"Recorded {args.outcome}: {args.note[:200]}")
    else:
        print(f"Recorded {args.outcome}.")
    return 0


def cmd_config(args) -> int:
    path = state_dir() / "config.json"
    saved = read_json(path, {})
    for item in args.set or []:
        name, _, raw = item.partition("=")
        if name not in INTS:
            die(f"unknown setting {name!r}; known: {', '.join(sorted(INTS))}")
        try:
            value = int(raw)
        except ValueError:
            die(f"{name} must be a whole number")
        if value < 0 or (name.endswith("_hour") and value > 23):
            die(f"{name} is out of range")
        saved[name] = value
    if args.set:
        write_json(path, saved)
    print(json.dumps(load_config(), indent=2, sort_keys=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Budget, memory and posting for the GACA heartbeat")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("gate", help="should this run happen?")
    context = sub.add_parser("context", help="what you told the user lately and how they reacted")
    context.add_argument("--days", type=int)
    sub.add_parser("alert", help="post one alert (JSON on stdin) through the caps")
    done = sub.add_parser("done", help="record how the run ended")
    done.add_argument("--outcome", required=True, choices=["quiet", "alerted", "error", "rate_limited"])
    done.add_argument("--note")
    config = sub.add_parser("config", help="show or change the budget settings")
    config.add_argument("--set", action="append", metavar="key=value")
    args = parser.parse_args()
    return {"gate": cmd_gate, "context": cmd_context, "alert": cmd_alert,
            "done": cmd_done, "config": cmd_config}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
