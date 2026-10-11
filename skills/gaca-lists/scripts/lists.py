#!/usr/bin/env python3
"""Helper for the gaca-lists skill: keeps the user's lists in GACA. Standard library only.

  overview                                  every list with counts
  show NAME [--all]                         one list, open items grouped by category
  add NAME ITEM... [--create] [--kind K] [--category C] [--for WHO] [--qty Q] [--note N]
  add NAME --items-json '[{"text":"...","category":"..."}]' [--create] [--kind K]
  check NAME ITEM...                        check items off (by name, loosely matched)
  uncheck NAME ITEM...
  remove NAME ITEM...
  clear-done NAME                           delete the checked-off items
  new NAME [--kind K] [--notes N]
  rename NAME NEW_NAME
  archive NAME
  delete NAME --yes                         delete a whole list and its items

NAME is matched loosely ("the costco run" finds "Costco"). The token comes from
GACA_API_TOKEN and is never printed. Exit codes: 0 done, 3 needs a decision (no matching
list, or several matched equally — the message says which), 1 a real error.
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

BASE = os.environ.get("GACA_BASE_URL", "http://127.0.0.1:41700").rstrip("/")
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131 Safari/537.36 Hermes-Lists/1.0"


def die(message: str, code: int = 1):
    print(message, file=sys.stderr)
    raise SystemExit(code)


def decide(message: str):
    """Not an error: the agent has to ask the user, or choose, before going on."""
    print(message)
    raise SystemExit(3)


def token() -> str:
    value = os.environ.get("GACA_API_TOKEN", "").strip()
    if not value:
        die("GACA_API_TOKEN is not configured")
    if "\n" in value or "\r" in value:
        die("GACA_API_TOKEN contains an invalid newline")
    return value


def api(method: str, path: str, payload: object | None = None):
    parsed = urllib.parse.urlparse(BASE)
    if parsed.scheme != "https" and parsed.hostname not in ("localhost", "127.0.0.1"):
        die("GACA_BASE_URL must be https (http is only allowed for localhost)")
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT, "Authorization": f"Bearer {token()}"}
    body = None
    if payload is not None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        headers["Content-Type"] = "application/json"
    for attempt in range(3):
        req = urllib.request.Request(f"{BASE}/api{path}", data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")
            try:
                detail = json.loads(detail).get("detail", detail)
            except (ValueError, AttributeError):
                pass
            if error.code in (401, 403):
                die("GACA rejected the token (401/403) — the user needs to mint a new GACA_API_TOKEN")
            if error.code < 500 or attempt == 2:
                die(f"{method} {path} failed ({error.code}): {str(detail)[:300]}")
        except urllib.error.URLError as error:
            if attempt == 2:
                die(f"Network error calling GACA: {error.reason}")
        time.sleep(2**attempt)


# ── Resolving names ──────────────────────────────────────────────────────────
def names(lists: list[dict]) -> str:
    return ", ".join(f"{l['name']} ({l['open_count']} open)" for l in lists) or "none yet"


def clean_new_name(name: str) -> str:
    """"the costco list" -> "costco": how someone refers to a list isn't what it's called.
    Only used when creating one. (The agent should still pass a clean, nicely cased name.)"""
    cleaned = re.sub(r"^(the|my|our)\s+", "", name.strip(), flags=re.I)
    cleaned = re.sub(r"\s+list$", "", cleaned, flags=re.I).strip()
    return cleaned or name.strip()


def plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def resolve_list(name: str, create: bool = False, kind: str = "general") -> dict:
    found = api("GET", "/lists/match?" + urllib.parse.urlencode({"q": name}))
    if found["match"]:
        return found["match"]
    if found["candidates"]:
        decide(f"AMBIGUOUS: {name!r} fits more than one list — ask which: " + names(found["candidates"]))
    if create:
        made = api("POST", "/lists", {"name": clean_new_name(name), "kind": kind})
        print(f"(created a new {kind} list called {made['name']!r})")
        return made
    everything = api("GET", "/lists")["lists"]
    decide(f"NO LIST matches {name!r}. Your lists: {names(everything)}. "
           "Create it by repeating the command with --create, or ask the user which they meant.")


# ── Formatting ───────────────────────────────────────────────────────────────
def describe(item: dict) -> str:
    text = item["text"] + (f" ({item['quantity']})" if item.get("quantity") else "")
    extras = [x for x in (f"for {item['for_person']}" if item.get("for_person") else None, item.get("price"),
                          item.get("note"), item.get("url")) if x]
    return text + (" — " + " — ".join(extras) if extras else "")


def show_text(detail: dict, include_done: bool) -> str:
    out = [f"{detail['name']} [{detail['kind']}] — {detail['open_count']} open, {detail['done_count']} checked off"]
    if detail.get("notes"):
        out.append(f"notes: {detail['notes']}")
    open_items = [i for i in detail["items"] if not i["done"]]
    done_items = [i for i in detail["items"] if i["done"]]
    if not open_items:
        out.append("(nothing left to get)" if detail["done_count"] else "(empty)")
    group = object()
    for item in open_items:
        category = item.get("category")
        if category != group:
            group = category
            out.append(f"{category or 'uncategorized'}:" if any(i.get("category") for i in open_items) else "")
        out.append(f"  - {describe(item)}")
    if include_done and done_items:
        out.append("checked off:")
        out += [f"  - {describe(i)}" for i in done_items]
    return "\n".join(line for line in out if line != "")


def parse_items(args) -> list[dict]:
    if args.items_json:
        try:
            raw = json.loads(args.items_json)
        except json.JSONDecodeError as error:
            die(f"--items-json isn't valid JSON: {error}")
        if not isinstance(raw, list) or not all(isinstance(i, dict) and i.get("text") for i in raw):
            die("--items-json must be a list of objects, each with a 'text'")
        return raw
    out = []
    for text in args.items:
        item = {"text": text}
        for flag, field in (("category", "category"), ("for_person", "for_person"), ("qty", "quantity"), ("note", "note")):
            if getattr(args, flag):
                item[field] = getattr(args, flag)
        out.append(item)
    return out


# ── Commands ─────────────────────────────────────────────────────────────────
def cmd_overview(args):
    lists = api("GET", "/lists")["lists"]
    if args.json:
        return print(json.dumps(lists, indent=1))
    if not lists:
        return print("No lists yet.")
    for l in lists:
        print(f"- {l['name']} [{l['kind']}]: {l['open_count']} open, {l['done_count']} checked off")


def cmd_show(args):
    target = resolve_list(args.name)
    detail = api("GET", f"/lists/{target['id']}")
    print(json.dumps(detail, indent=1) if args.json else show_text(detail, args.all))


def cmd_add(args):
    items = parse_items(args)
    if not items:
        die("Nothing to add: give item names, or --items-json")
    target = resolve_list(args.name, create=args.create, kind=args.kind)
    result = api("POST", f"/lists/{target['id']}/items", {"items": items})
    if args.json:
        return print(json.dumps(result, indent=1))
    lines = []
    if result["added"]:
        lines.append("Added: " + ", ".join(describe(i) for i in result["added"]))
    if result["reopened"]:
        lines.append("Un-checked (they were already on the list, checked off): " + ", ".join(i["text"] for i in result["reopened"]))
    if result["duplicates"]:
        lines.append("Already on the list, left as is: " + ", ".join(f"{d['text']} (= {d['existing']['text']})" for d in result["duplicates"]))
    related = [f"{i['text']} (similar to existing '{i['similar_to']}')" for i in result["added"] if i.get("similar_to")]
    if related:
        lines.append("Heads-up, may be a duplicate: " + "; ".join(related))
    l = result["list"]
    lines.append(f"{l['name']} now has {plural(l['open_count'], 'open item')}.")
    print("\n".join(lines))


def _report_by_text(verb: str, result: dict, name: str):
    lines = []
    if result["matched"]:
        lines.append(f"{verb}: " + ", ".join(i["text"] for i in result["matched"]))
    for a in result["ambiguous"]:
        lines.append(f"AMBIGUOUS {a['query']!r} — could be: " + ", ".join(c["text"] for c in a["candidates"]) + " (left unchanged; ask which)")
    if result["not_found"]:
        lines.append("Not found on " + name + ": " + ", ".join(repr(t) for t in result["not_found"]))
    print("\n".join(lines) or "Nothing changed.")
    if result["ambiguous"]:
        raise SystemExit(3)


def cmd_check(args, done=True):
    target = resolve_list(args.name)
    result = api("POST", f"/lists/{target['id']}/items/check", {"texts": args.items, "done": done})
    _report_by_text("Checked off" if done else "Un-checked", result, target["name"])


def cmd_uncheck(args):
    cmd_check(args, done=False)


def cmd_remove(args):
    target = resolve_list(args.name)
    _report_by_text("Removed", api("POST", f"/lists/{target['id']}/items/remove", {"texts": args.items}), target["name"])


def cmd_clear_done(args):
    target = resolve_list(args.name)
    removed = api("POST", f"/lists/{target['id']}/clear-done")["removed"]
    print(f"Cleared {removed} checked-off item(s) from {target['name']}.")


def cmd_new(args):
    made = api("POST", "/lists", {"name": args.name, "kind": args.kind, **({"notes": args.notes} if args.notes else {})})
    print(f"Created {made['kind']} list {made['name']!r}.")


def cmd_rename(args):
    target = resolve_list(args.name)
    renamed = api("PATCH", f"/lists/{target['id']}", {"name": args.new_name})
    print(f"Renamed {target['name']!r} to {renamed['name']!r}.")


def cmd_archive(args):
    target = resolve_list(args.name)
    api("PATCH", f"/lists/{target['id']}", {"archived": True})
    print(f"Archived {target['name']!r} (its items are kept; it's just out of the way).")


def cmd_delete(args):
    target = resolve_list(args.name)
    if not args.yes:
        decide(f"Deleting {target['name']!r} permanently removes its {target['open_count'] + target['done_count']} items. "
               "Confirm with the user, then repeat with --yes. (Archiving keeps everything.)")
    api("DELETE", f"/lists/{target['id']}")
    print(f"Deleted {target['name']!r}.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", action="store_true", help="print raw JSON (overview/show/add)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("overview").set_defaults(fn=cmd_overview)
    p = sub.add_parser("show"); p.add_argument("name"); p.add_argument("--all", action="store_true"); p.set_defaults(fn=cmd_show)
    p = sub.add_parser("add"); p.add_argument("name"); p.add_argument("items", nargs="*")
    p.add_argument("--create", action="store_true"); p.add_argument("--kind", default="general", choices=["shopping", "gift", "todo", "general"])
    p.add_argument("--category"); p.add_argument("--for", dest="for_person"); p.add_argument("--qty"); p.add_argument("--note")
    p.add_argument("--items-json"); p.set_defaults(fn=cmd_add)
    for name, fn in (("check", cmd_check), ("uncheck", cmd_uncheck), ("remove", cmd_remove)):
        p = sub.add_parser(name); p.add_argument("name"); p.add_argument("items", nargs="+"); p.set_defaults(fn=fn)
    p = sub.add_parser("clear-done"); p.add_argument("name"); p.set_defaults(fn=cmd_clear_done)
    p = sub.add_parser("new"); p.add_argument("name"); p.add_argument("--kind", default="general", choices=["shopping", "gift", "todo", "general"])
    p.add_argument("--notes"); p.set_defaults(fn=cmd_new)
    p = sub.add_parser("rename"); p.add_argument("name"); p.add_argument("new_name"); p.set_defaults(fn=cmd_rename)
    p = sub.add_parser("archive"); p.add_argument("name"); p.set_defaults(fn=cmd_archive)
    p = sub.add_parser("delete"); p.add_argument("name"); p.add_argument("--yes", action="store_true"); p.set_defaults(fn=cmd_delete)

    args = parser.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
