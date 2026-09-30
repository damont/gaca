#!/usr/bin/env python3
"""Helper for the david-daily-briefing skill. Standard library only.

Deterministic pieces of the morning briefing, so the agent only has to make the
judgment calls (where is David today? what are the important headlines?):

  today                      print today's date in America/New_York
  calendar [--date D] [--days N]
                             JSON: family-calendar items for D..D+N-1 (reasoning input)
  calendar-section [--date D]
                             JSON: a postable `calendar` section for day D
  weather --place "City, ST" [--date D]
                             JSON: a postable `weather` section for that place
  sports [--date D]          JSON: a postable `sports` section (Clemson, NBA, college football)
  post --date D --key K      read a section JSON on stdin and PUT it to GACA
                             (an empty section is DELETEd instead — no blank cards)
  summary --date D           read summary text on stdin and PUT it to GACA
  finish --date D [--problem TEXT ...]
                             post the "briefing is ready" alert to GACA

Secrets come from the environment (GACA_API_TOKEN, CALENDAR_API_TOKEN) and are
never printed. Dates are YYYY-MM-DD in the user's local time (America/New_York).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/New_York")
CALENDAR_BASE = "https://calendar.rabidflamingos.com"
GACA_BASE = os.environ.get("GACA_BASE_URL", "https://goodandcheapagent.com").rstrip("/")
# Cloudflare rejects Python's default signature on the calendar host (Error 1010).
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "Chrome/131 Safari/537.36 Hermes-Daily-Briefing/1.0"
)
ESPN = "https://site.api.espn.com/apis/site/v2/sports"
CLEMSON_ID = "228"

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

# WMO weather interpretation codes -> words (Open-Meteo).
WMO = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "freezing fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    56: "freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light showers", 81: "showers", 82: "heavy showers", 85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorms", 96: "thunderstorms with hail", 99: "severe thunderstorms with hail",
}

US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts",
    "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
    "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin",
    "WY": "Wyoming", "DC": "District of Columbia",
}


# ── HTTP ─────────────────────────────────────────────────────────────────────
def die(message: str, code: int = 1) -> "NoReturn":  # noqa: F821
    print(message, file=sys.stderr)
    raise SystemExit(code)


def env_token(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        die(f"{name} is not configured")
    if "\n" in value or "\r" in value:
        die(f"{name} contains an invalid newline")
    return value


def request_json(method: str, url: str, *, token: str | None = None, payload: object | None = None):
    """JSON request with retries on 5xx/network errors. Returns (status, body)."""
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = None
    if payload is not None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    for attempt in range(3):
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                raw = response.read()
                return response.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:500]
            if error.code < 500 or attempt == 2:
                die(f"{method} {urllib.parse.urlsplit(url).path} returned HTTP {error.code}: {detail}")
        except urllib.error.URLError as error:
            if attempt == 2:
                die(f"Network error calling {urllib.parse.urlsplit(url).netloc}: {error.reason}")
        time.sleep(2**attempt)
    die("unreachable")


def curl_json(url: str):
    """GET JSON via curl. ESPN (and similar) reject Python's TLS/header signature."""
    for attempt in range(3):
        result = subprocess.run(
            ["curl", "-sf", "--max-time", "20", "-H", "Accept: application/json", url],
            capture_output=True,
        )
        if result.returncode == 0 and result.stdout:
            try:
                return json.loads(result.stdout)
            except json.JSONDecodeError:
                pass
        time.sleep(2**attempt)
    die(f"Could not fetch {urllib.parse.urlsplit(url).netloc}{urllib.parse.urlsplit(url).path}")


# ── Dates ────────────────────────────────────────────────────────────────────
def today_ny() -> date:
    return datetime.now(TZ).date()


def parse_day_arg(value: str | None) -> date:
    if not value:
        return today_ny()
    try:
        return date.fromisoformat(value)
    except ValueError:
        die(f"Bad date {value!r}; expected YYYY-MM-DD")


def fmt_time(iso_utc: str) -> str:
    """'2026-10-03T23:30Z' -> 'Sat 7:30 PM' in New York."""
    dt = datetime.fromisoformat(iso_utc.replace("Z", "+00:00")).astimezone(TZ)
    return f"{dt.strftime('%a')} {dt.strftime('%I:%M %p').lstrip('0')}"


# ── Calendar ─────────────────────────────────────────────────────────────────
def day_to_date(day_value: object, week_start: date) -> date | None:
    """Resolve the calendar's free-form `day` ('saturday', 'Monday',
    'Friday, October 2') to a date within the week starting at `week_start`."""
    if not isinstance(day_value, str) or not day_value.strip():
        return None
    text = day_value.strip()
    head = text.split(",", 1)[0].strip().casefold()
    if head not in WEEKDAYS:
        return None
    offset = WEEKDAYS.index(head)
    # Mondays are week starts, so the offset lands inside the week. A full
    # "Weekday, Month D" is trusted only when it agrees with that position.
    candidate = week_start + timedelta(days=offset)
    if "," in text:
        try:
            parsed = datetime.strptime(f"{text.title()} {candidate.year}", "%A, %B %d %Y")
        except ValueError:
            return candidate
        if (parsed.month, parsed.day) != (candidate.month, candidate.day):
            return None
    return candidate


def calendar_items_by_day(start: date, days: int) -> tuple[dict[date, list[dict]], list[str]]:
    token = env_token("CALENDAR_API_TOKEN")
    end = start + timedelta(days=days)
    utc = ZoneInfo("UTC")
    query = urllib.parse.urlencode({
        "start_date": datetime.combine(start, datetime.min.time(), TZ).astimezone(utc).isoformat().replace("+00:00", "Z"),
        "end_date": datetime.combine(end, datetime.min.time(), TZ).astimezone(utc).isoformat().replace("+00:00", "Z"),
    })
    request_json("GET", f"{CALENDAR_BASE}/api/auth/me", token=token)  # confirms token + account
    _, weeks = request_json("GET", f"{CALENDAR_BASE}/api/weeks?{query}", token=token)
    if not isinstance(weeks, list):
        die("Calendar returned an unexpected response")

    by_day: dict[date, list[dict]] = {}
    notes: list[str] = []
    for week in weeks:
        if not isinstance(week, dict):
            continue
        week_start = date.fromisoformat(str(week.get("week_start", ""))[:10])
        if week.get("notes"):
            notes.append(str(week["notes"]))
        for field, category in (("weekday_events", "event"), ("weekend_plans", "plan")):
            for item in week.get(field) or []:
                day = day_to_date(item.get("day"), week_start)
                if day is None:
                    continue
                by_day.setdefault(day, []).append({
                    "category": category,
                    "title": str(item.get("title") or "Untitled").strip(),
                    "time": (item.get("time") or "").strip() or None,
                    "description": (item.get("description") or "").strip() or None,
                })
        for item in week.get("sports") or []:
            day = day_to_date(item.get("day"), week_start)
            if day is None:
                continue
            child = str(item.get("child_name") or "").strip()
            sport = str(item.get("sport") or "Game").strip()
            by_day.setdefault(day, []).append({
                "category": "sports",
                "title": f"{child}: {sport}" if child else sport,
                "time": (item.get("time") or "").strip() or None,
                "location": (item.get("location") or "").strip() or None,
                "description": None,
            })
    return by_day, notes


def cmd_calendar(args) -> None:
    start = parse_day_arg(args.date)
    by_day, notes = calendar_items_by_day(start, args.days)
    out = {
        "start": start.isoformat(),
        "days": [
            {
                "date": (start + timedelta(days=i)).isoformat(),
                "weekday": (start + timedelta(days=i)).strftime("%A"),
                "items": by_day.get(start + timedelta(days=i), []),
            }
            for i in range(args.days)
        ],
        "week_notes": notes,
    }
    print(json.dumps(out, indent=2))


def cmd_calendar_section(args) -> None:
    day = parse_day_arg(args.date)
    by_day, _ = calendar_items_by_day(day, 1)
    raw = by_day.get(day, [])
    # Times are free text ("Afternoon", "4:15 PM"), so keep the calendar's own order.
    items = []
    for it in raw:
        items.append({
            "title": it["title"],
            **({"time": it["time"]} if it.get("time") else {}),
            **({"location": it["location"]} if it.get("location") else {}),
            **({"body": it["description"]} if it.get("description") else {}),
            "priority": "high" if it["category"] == "sports" else "medium",
        })
    n = len(items)
    section = {
        "title": "Family calendar",
        "kind": "calendar",
        "headline": (f"{n} thing{'s' if n != 1 else ''} on the calendar" if n else "Nothing on the calendar"),
        "display_order": 1,
        "items": items,
    }
    # An empty day still gets a headline-only card ("Nothing on the calendar").
    print(json.dumps(section, indent=2))


# ── Weather ──────────────────────────────────────────────────────────────────
def geocode(place: str) -> dict:
    parts = [p.strip() for p in place.split(",") if p.strip()]
    if not parts:
        die("--place is required")
    name, hint = parts[0], " ".join(parts[1:]).strip()
    url = "https://geocoding-api.open-meteo.com/v1/search?" + urllib.parse.urlencode(
        {"name": name, "count": 10, "language": "en", "format": "json"}
    )
    results = curl_json(url).get("results") or []
    if not results:
        die(f"No location found for {place!r}")
    if hint:
        want = US_STATES.get(hint.upper(), hint).casefold()
        for r in results:
            if want in (str(r.get("admin1", "")).casefold(), str(r.get("country", "")).casefold(),
                        str(r.get("country_code", "")).casefold()):
                return r
    return results[0]  # Open-Meteo orders by relevance/population


def cmd_weather(args) -> None:
    target = parse_day_arg(args.date)
    geo = geocode(args.place)
    label = ", ".join(x for x in (geo["name"], geo.get("admin1") or geo.get("country")) if x)
    url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode({
        "latitude": geo["latitude"], "longitude": geo["longitude"],
        "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,wind_speed_10m_max",
        "temperature_unit": "fahrenheit", "wind_speed_unit": "mph", "timezone": geo.get("timezone", "auto"),
        "start_date": target.isoformat(),
        "end_date": (target + timedelta(days=1)).isoformat(),
    })
    data = curl_json(url)
    daily = data["daily"]

    def describe(i: int) -> tuple[str, str]:
        sky = WMO.get(daily["weather_code"][i], "mixed conditions")
        hi, lo = round(daily["temperature_2m_max"][i]), round(daily["temperature_2m_min"][i])
        rain = daily["precipitation_probability_max"][i]
        wind = round(daily["wind_speed_10m_max"][i])
        detail = f"High {hi}° / low {lo}°"
        if rain is not None:
            detail += f" · {rain}% chance of precipitation"
        detail += f" · winds up to {wind} mph"
        return sky, detail

    sky, detail = describe(0)
    now_temp = None
    if target == today_ny() and data.get("current"):
        now_temp = round(data["current"]["temperature_2m"])
    hi0, lo0 = round(daily["temperature_2m_max"][0]), round(daily["temperature_2m_min"][0])
    headline = f"{sky.capitalize()}, high {hi0}°" + (f", {now_temp}° now" if now_temp is not None else "")
    items = [{"title": f"{'Today' if target == today_ny() else target.strftime('%A')} in {label}",
              "body": detail, "priority": "high" if (daily["precipitation_probability_max"][0] or 0) >= 60 else "medium"}]
    if len(daily["weather_code"]) > 1:
        sky1, detail1 = describe(1)
        items.append({"title": f"{'Tomorrow' if target == today_ny() else 'Next day'}: {sky1}", "body": detail1, "priority": "low"})
    print(json.dumps({
        "title": f"Weather · {label}", "kind": "weather", "headline": headline,
        "display_order": 2, "items": items,
    }, indent=2))


# ── Sports (ESPN public scoreboard/schedule JSON) ────────────────────────────
def score_of(competitor: dict) -> str | None:
    score = competitor.get("score")
    if isinstance(score, dict):
        score = score.get("displayValue")
    return str(score) if score not in (None, "") else None


def broadcast_names(competition: dict) -> str:
    names: list[str] = []
    for b in competition.get("broadcasts") or []:
        if "names" in b:
            names += b["names"]
        elif isinstance(b.get("media"), dict) and b["media"].get("shortName"):
            names.append(b["media"]["shortName"])
    return ", ".join(dict.fromkeys(names))


def team_games(sport_path: str, label: str, today: date) -> list[dict]:
    """Clemson's next game (and last result, if recent) for one sport."""
    data = curl_json(f"{ESPN}/{sport_path}/teams/{CLEMSON_ID}/schedule")
    events = data.get("events") or []
    last = None
    upcoming = None
    for ev in events:
        comp = ev["competitions"][0]
        state = comp["status"]["type"]["state"]
        when = datetime.fromisoformat(ev["date"].replace("Z", "+00:00")).astimezone(TZ)
        if state == "post":
            last = (ev, comp, when)
        elif state in ("pre", "in") and upcoming is None and when.date() >= today:
            upcoming = (ev, comp, when)
    out: list[dict] = []
    if upcoming:
        ev, comp, when = upcoming
        us = next(c for c in comp["competitors"] if c["team"]["id"] == CLEMSON_ID)
        them = next(c for c in comp["competitors"] if c["team"]["id"] != CLEMSON_ID)
        prefix = "vs" if us["homeAway"] == "home" else "at"
        days_away = (when.date() - today).days
        body = None
        if last:
            lev, lcomp, lwhen = last
            lus = next(c for c in lcomp["competitors"] if c["team"]["id"] == CLEMSON_ID)
            lthem = next(c for c in lcomp["competitors"] if c["team"]["id"] != CLEMSON_ID)
            if (today - lwhen.date()).days <= 10:
                lpref = "vs" if lus["homeAway"] == "home" else "at"
                result = "W" if lus.get("winner") else "L"
                body = f"Last game: {result} {score_of(lus)}–{score_of(lthem)} {lpref} {lthem['team']['shortDisplayName']}"
        tv = broadcast_names(comp)
        out.append({
            "title": f"{label}: {prefix} {them['team']['shortDisplayName']}",
            "time": f"{fmt_time(ev['date'])} ET" + (f" · {tv}" if tv else "")
                    + ("" if days_away > 1 else " (today)" if days_away == 0 else " (tomorrow)"),
            **({"body": body} if body else {}),
            "priority": "high" if days_away <= 1 else "medium" if days_away <= 7 else "low",
        })
    elif last and (today - last[2].date()).days <= 3:
        ev, comp, when = last
        us = next(c for c in comp["competitors"] if c["team"]["id"] == CLEMSON_ID)
        them = next(c for c in comp["competitors"] if c["team"]["id"] != CLEMSON_ID)
        pref = "vs" if us["homeAway"] == "home" else "at"
        out.append({
            "title": f"{label}: {'W' if us.get('winner') else 'L'} {score_of(us)}–{score_of(them)} {pref} {them['team']['shortDisplayName']}",
            "priority": "medium",
        })
    return out


def scoreboard(sport_path: str, day: date, extra: str = "") -> list[dict]:
    data = curl_json(f"{ESPN}/{sport_path}/scoreboard?dates={day.strftime('%Y%m%d')}{extra}")
    return data.get("events") or []


def line_for_game(ev: dict, *, show_rank: bool) -> str:
    comp = ev["competitions"][0]
    away = next(c for c in comp["competitors"] if c["homeAway"] == "away")
    home = next(c for c in comp["competitors"] if c["homeAway"] == "home")

    def name(c: dict) -> str:
        rank = (c.get("curatedRank") or {}).get("current")
        tag = f"#{rank} " if show_rank and rank and rank <= 25 else ""
        return f"{tag}{c['team']['shortDisplayName']}"

    state = comp["status"]["type"]["state"]
    if state == "pre":
        tv = broadcast_names(comp)
        return f"{name(away)} at {name(home)} — {fmt_time(ev['date'])}" + (f" ({tv})" if tv else "")
    final = "Final" if comp["status"]["type"].get("completed") else comp["status"]["type"].get("shortDetail", "")
    return f"{name(away)} {score_of(away)}, {name(home)} {score_of(home)} ({final})"


def ranked(ev: dict) -> bool:
    return any(((c.get("curatedRank") or {}).get("current") or 99) <= 25
               for c in ev["competitions"][0]["competitors"])


def cmd_sports(args) -> None:
    today = parse_day_arg(args.date)
    items: list[dict] = []
    headline = None
    problems: list[str] = []

    # Clemson: football and men's basketball (basketball is empty in the offseason).
    for path, label in (("football/college-football", "Clemson football"),
                        ("basketball/mens-college-basketball", "Clemson basketball")):
        try:
            games = team_games(path, label, today)
        except (SystemExit, KeyError, StopIteration) as exc:
            problems.append(f"{label}: {exc}")
            continue
        items.extend(games)
        if games and headline is None and games[0]["priority"] in ("high", "medium"):
            headline = f"{games[0]['title']} — {games[0]['time']}"

    # NBA: last night's finals and today's slate.
    try:
        last_night = [e for e in scoreboard("basketball/nba", today - timedelta(days=1))
                      if e["competitions"][0]["status"]["type"].get("completed")]
        tonight = scoreboard("basketball/nba", today)
        if last_night:
            items.append({"title": "NBA last night",
                          "body": "\n".join(line_for_game(e, show_rank=False) for e in last_night[:12]),
                          "priority": "low"})
        upcoming = [e for e in tonight if e["competitions"][0]["status"]["type"]["state"] == "pre"]
        if upcoming:
            items.append({"title": "NBA tonight",
                          "body": "\n".join(line_for_game(e, show_rank=False) for e in upcoming[:12]),
                          "priority": "low"})
    except (SystemExit, KeyError) as exc:
        problems.append(f"NBA: {exc}")

    # College football: ranked games. Sun-Tue show the weekend's results; otherwise the coming slate.
    try:
        weekday = today.weekday()  # Mon=0 .. Sun=6
        if weekday in (6, 0, 1):  # Sun, Mon, Tue -> last Saturday's results
            sat = today - timedelta(days=(weekday - 5) % 7)
            label, day, want_final = "Top-25 college football results", sat, True
        else:
            sat = today + timedelta(days=(5 - weekday) % 7) if weekday != 5 else today
            label, day, want_final = f"Top-25 college football — {sat.strftime('%a %b %d').replace(' 0', ' ')}", sat, False
        games = [e for e in scoreboard("football/college-football", day, "&groups=80&limit=300") if ranked(e)]
        if want_final:
            games = [e for e in games if e["competitions"][0]["status"]["type"].get("completed")]
        if games:
            items.append({"title": label,
                          "body": "\n".join(line_for_game(e, show_rank=True) for e in games[:10]),
                          "priority": "low"})
    except (SystemExit, KeyError) as exc:
        problems.append(f"College football: {exc}")

    if problems:
        print("sports: partial data — " + "; ".join(problems), file=sys.stderr)
    print(json.dumps({
        "title": "Sports", "kind": "sports",
        "headline": headline or ("Nothing on the slate" if not items else None),
        "display_order": 3, "items": items,
    }, indent=2))


# ── Posting to GACA ──────────────────────────────────────────────────────────
def cmd_post(args) -> None:
    day = parse_day_arg(args.date)
    token = env_token("GACA_API_TOKEN")
    section = json.load(sys.stdin)
    if not isinstance(section, dict):
        die("Section JSON must be an object")
    path = f"{GACA_BASE}/api/briefings/{day.isoformat()}/sections/{args.key}"
    if not section.get("items") and not section.get("headline"):
        try:
            request_json("DELETE", path, token=token)
            print(f"{args.key}: empty, removed")
        except SystemExit:
            print(f"{args.key}: empty, nothing to remove")
        return
    _, out = request_json("PUT", path, token=token, payload=section)
    print(f"{args.key}: posted ({len(out.get('items', []))} items)")


def cmd_summary(args) -> None:
    day = parse_day_arg(args.date)
    token = env_token("GACA_API_TOKEN")
    text = (args.text if args.text is not None else sys.stdin.read()).strip()
    request_json("PUT", f"{GACA_BASE}/api/briefings/{day.isoformat()}", token=token, payload={"summary": text or None})
    print("summary: posted")


def cmd_finish(args) -> None:
    day = parse_day_arg(args.date)
    token = env_token("GACA_API_TOKEN")
    _, briefing = request_json("GET", f"{GACA_BASE}/api/briefings?date={day.isoformat()}", token=token)
    sections = briefing.get("sections", [])
    if not sections and not briefing.get("summary"):
        die("Briefing is empty; not sending a 'ready' alert", 2)
    parts = [f"{s['title']} ({len(s.get('items', []))})" if s.get("items") else s["title"] for s in sections]
    body = "Posted: " + "; ".join(parts) + "."
    if args.problem:
        body += "\nProblems: " + "; ".join(args.problem)
    payload = {
        "source": "daily-briefing",
        "kind": "briefing.posted",
        "severity": "warn" if args.problem else "info",
        "title": ("Daily briefing ready (with gaps)" if args.problem else "Daily briefing ready"),
        "body": body,
        "data": {"date": day.isoformat(), "sections": [s["key"] for s in sections]},
        "dedupe_key": f"briefing:posted:{day.isoformat()}",
        "suggested_actions": [],
    }
    _, created = request_json("POST", f"{GACA_BASE}/api/alerts", token=token, payload=payload)
    print(f"alert: {created.get('id', '?') if isinstance(created, dict) else '?'}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("today").set_defaults(fn=lambda a: print(today_ny().isoformat()))

    p = sub.add_parser("calendar"); p.add_argument("--date"); p.add_argument("--days", type=int, default=4)
    p.set_defaults(fn=cmd_calendar)
    p = sub.add_parser("calendar-section"); p.add_argument("--date"); p.set_defaults(fn=cmd_calendar_section)
    p = sub.add_parser("weather"); p.add_argument("--place", required=True); p.add_argument("--date")
    p.set_defaults(fn=cmd_weather)
    p = sub.add_parser("sports"); p.add_argument("--date"); p.set_defaults(fn=cmd_sports)
    p = sub.add_parser("post"); p.add_argument("--date", required=True)
    p.add_argument("--key", required=True, choices=["calendar", "weather", "sports", "news"] )
    p.set_defaults(fn=cmd_post)
    p = sub.add_parser("summary"); p.add_argument("--date", required=True); p.add_argument("--text")
    p.set_defaults(fn=cmd_summary)
    p = sub.add_parser("finish"); p.add_argument("--date", required=True)
    p.add_argument("--problem", action="append", default=[]); p.set_defaults(fn=cmd_finish)

    args = parser.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
