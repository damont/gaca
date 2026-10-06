#!/usr/bin/env python3
"""Helper for the gaca-artifacts skill: publishes HTML pages ("artifacts") to GACA and reads or
writes the data they keep. Standard library only.

  list [--q TEXT] [--all]                      the user's artifacts
  show REF                                     details, data notes, and a summary of stored data
  publish --title T --html-file F [--description D] [--data-notes N] [--crew C]
          [--alert ID] [--session ID] [--state-file F] [--collection NAME=FILE]...
  update REF [--html-file F] [--title T] [--description D] [--data-notes N] [--note N]
  html REF --out FILE [--version N]            download the current page (to edit and republish)
  data REF                                     everything the page stores, as JSON
  state REF                                    just the state document
  state-set REF --json J | --file F            replace the state
  state-patch REF --json J | --file F          merge into the state (null deletes a key)
  docs REF COLLECTION                          list a collection's documents
  add REF COLLECTION --json J | --file F [--id ID]
  patch REF COLLECTION ID --json J             merge fields into a document
  remove REF COLLECTION ID  /  clear REF COLLECTION
  events REF [--unhandled] [--after N]         what the page told the agent (button presses...)
  handled REF UPTO                             mark events up to seq UPTO as handled
  versions REF  /  restore REF VERSION  /  archive REF  /  delete REF --yes

REF is an artifact id (32 hex characters) or part of its title ("costco"). The token comes from
GACA_API_TOKEN and is never printed. Exit codes: 0 done, 3 needs a decision (no match, or several
artifacts matched — the message lists them), 1 a real error.
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

BASE = os.environ.get("GACA_BASE_URL", "https://goodandcheapagent.com").rstrip("/")
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131 Safari/537.36 Hermes-Artifacts/1.0"
ID_RE = re.compile(r"^[0-9a-f]{32}$")


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


def api(method: str, path: str, payload: object | None = None, raw: bool = False):
    parsed = urllib.parse.urlparse(BASE)
    if parsed.scheme != "https" and parsed.hostname not in ("localhost", "127.0.0.1"):
        die("GACA_BASE_URL must be https (http is only allowed for localhost)")
    headers = {"Accept": "*/*" if raw else "application/json", "User-Agent": USER_AGENT,
               "Authorization": f"Bearer {token()}"}
    body = None
    if payload is not None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        headers["Content-Type"] = "application/json"
    for attempt in range(3):
        req = urllib.request.Request(f"{BASE}/api{path}", data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                data = response.read()
                if raw:
                    return data.decode("utf-8")
                return json.loads(data) if data else None
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")
            try:
                detail = json.loads(detail).get("detail", detail)
            except (ValueError, AttributeError):
                pass
            if error.code in (401, 403):
                die("GACA rejected the token (401/403) — the user needs to mint a new GACA_API_TOKEN")
            if error.code == 409:
                die(f"CONFLICT ({method} {path}): {str(detail)[:300]}")
            if error.code < 500 or attempt == 2:
                die(f"{method} {path} failed ({error.code}): {str(detail)[:600]}")
        except urllib.error.URLError as error:
            if attempt == 2:
                die(f"Network error calling GACA: {error.reason}")
        time.sleep(2**attempt)


def q(value: str) -> str:
    return urllib.parse.quote(value, safe="")


# ── Resolving REF ────────────────────────────────────────────────────────────
def resolve(ref: str) -> str:
    """An artifact id, from an id or part of a title. Several matches -> ask; none -> say so."""
    if ID_RE.match(ref):
        return ref
    found = api("GET", "/artifacts?" + urllib.parse.urlencode({"q": ref}))["artifacts"]
    exact = [a for a in found if a["title"].lower() == ref.lower()]
    if len(exact) == 1:
        return exact[0]["id"]
    if len(found) == 1:
        return found[0]["id"]
    if found:
        decide(f"AMBIGUOUS: {ref!r} fits more than one artifact — ask which, or use an id:\n"
               + "\n".join(f"  {a['id']}  {a['title']}" for a in found))
    everything = api("GET", "/artifacts")["artifacts"]
    decide(f"NO ARTIFACT matches {ref!r}. Existing: "
           + (", ".join(a["title"] for a in everything) or "none yet"))


# ── Input helpers ────────────────────────────────────────────────────────────
def read_file(path: str) -> str:
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as f:
            return f.read()
    except OSError as error:
        die(f"Can't read {path}: {error.strerror}")


def json_arg(args, what: str = "data") -> object:
    text = args.json if args.json is not None else (read_file(args.file) if args.file else None)
    if text is None:
        die(f"Give the {what} with --json '{{...}}' or --file path.json")
    try:
        return json.loads(text)
    except ValueError as error:
        die(f"Invalid JSON for {what}: {error}")


def add_json_args(p):
    p.add_argument("--json")
    p.add_argument("--file")


def show_json(value) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False))


# ── Commands ─────────────────────────────────────────────────────────────────
def describe(a: dict) -> str:
    bits = [f"v{a['version']}", f"updated {a['updated_at'][:16].replace('T', ' ')}Z"]
    if a.get("crew"):
        bits.append(a["crew"])
    if a.get("archived"):
        bits.append("archived")
    line = f"{a['id']}  {a['title']}  [{', '.join(bits)}]"
    return line + (f"\n    {a['description']}" if a.get("description") else "")


def cmd_list(args):
    params = {"include_archived": "true" if args.all else "false"}
    if args.q:
        params["q"] = args.q
    found = api("GET", "/artifacts?" + urllib.parse.urlencode(params))["artifacts"]
    print("\n".join(describe(a) for a in found) if found else "(no artifacts)")


def cmd_show(args):
    aid = resolve(args.ref)
    a = api("GET", f"/artifacts/{aid}")
    print(describe(a))
    print(f"link: {a['link']}   (put this in a chat reply and the app shows a tappable chip)")
    if a.get("data_notes"):
        print(f"data notes: {a['data_notes']}")
    snap = api("GET", f"/artifacts/{aid}/data")
    state = snap.get("state") or {}
    print(f"state: {len(state)} top-level key(s)" + (f" — {', '.join(list(state)[:12])}" if state else ""))
    for name, docs in (snap.get("collections") or {}).items():
        print(f"collection {name}: {len(docs)} document(s)")
    events = api("GET", f"/artifacts/{aid}/events?unhandled=true")["events"]
    if events:
        print(f"{len(events)} unhandled event(s) from the page — run: events {aid} --unhandled")


def cmd_publish(args):
    body = {"title": args.title, "html": read_file(args.html_file)}
    for field, key in (("description", "description"), ("data_notes", "data_notes"), ("crew", "crew"),
                       ("alert", "alert_id"), ("session", "session_id")):
        if getattr(args, field):
            body[key] = getattr(args, field)
    if args.state_file:
        body["state"] = json.loads(read_file(args.state_file))
    collections = {}
    for spec in args.collection or []:
        name, _, path = spec.partition("=")
        if not name or not path:
            die("--collection takes NAME=FILE, where FILE holds {\"docId\": {...}, ...}")
        collections[name] = json.loads(read_file(path))
    if collections:
        body["collections"] = collections
    made = api("POST", "/artifacts", body)
    print(f"Published {made['title']!r} (v{made['version']}).")
    print(f"id: {made['id']}")
    print(f"link: {made['link']}")
    print("Include the link in your reply so the user gets a tappable chip, or mention it is in the Artifacts tab.")


def cmd_update(args):
    aid = resolve(args.ref)
    body = {}
    if args.html_file:
        body["html"] = read_file(args.html_file)
    for field in ("title", "description", "data_notes", "note"):
        if getattr(args, field) is not None:
            body[field] = getattr(args, field)
    if not body:
        die("Nothing to change — give --html-file, --title, --description, --data-notes or --note")
    made = api("PATCH", f"/artifacts/{aid}", body)
    print(f"Updated {made['title']!r} (now v{made['version']}). Stored data was not touched.")


def cmd_html(args):
    aid = resolve(args.ref)
    suffix = f"?version={args.version}" if args.version else ""
    html = api("GET", f"/artifacts/{aid}/html{suffix}", raw=True)
    with open(os.path.expanduser(args.out), "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Wrote {len(html)} bytes to {args.out}")


def cmd_data(args):
    show_json(api("GET", f"/artifacts/{resolve(args.ref)}/data"))


def cmd_state(args):
    show_json(api("GET", f"/artifacts/{resolve(args.ref)}/state"))


def cmd_state_set(args):
    r = api("PUT", f"/artifacts/{resolve(args.ref)}/state", {"state": json_arg(args, "state")})
    print(f"State replaced (version {r['version']}).")


def cmd_state_patch(args):
    r = api("PATCH", f"/artifacts/{resolve(args.ref)}/state", {"patch": json_arg(args, "patch")})
    print(f"State updated (version {r['version']}).")
    show_json(r["state"])


def cmd_docs(args):
    r = api("GET", f"/artifacts/{resolve(args.ref)}/collections/{q(args.collection)}")
    if not r["docs"]:
        print("(empty)")
    for d in r["docs"]:
        print(f"{d['id']} (v{d['version']}): {json.dumps(d['data'], ensure_ascii=False)}")


def cmd_add(args):
    body = {"data": json_arg(args)}
    if args.id:
        body["id"] = args.id
    d = api("POST", f"/artifacts/{resolve(args.ref)}/collections/{q(args.collection)}", body)
    print(f"Added {args.collection}/{d['id']}")


def cmd_patch(args):
    d = api("PATCH", f"/artifacts/{resolve(args.ref)}/collections/{q(args.collection)}/{q(args.id)}",
            {"patch": json_arg(args, "patch")})
    print(f"Updated {args.collection}/{d['id']} (v{d['version']}): {json.dumps(d['data'], ensure_ascii=False)}")


def cmd_remove(args):
    api("DELETE", f"/artifacts/{resolve(args.ref)}/collections/{q(args.collection)}/{q(args.id)}")
    print(f"Removed {args.collection}/{args.id}")


def cmd_clear(args):
    api("DELETE", f"/artifacts/{resolve(args.ref)}/collections/{q(args.collection)}")
    print(f"Cleared {args.collection}")


def cmd_events(args):
    params = {"after": args.after, "unhandled": "true" if args.unhandled else "false"}
    events = api("GET", f"/artifacts/{resolve(args.ref)}/events?" + urllib.parse.urlencode(params))["events"]
    if not events:
        print("(no events)")
    for e in events:
        print(f"#{e['seq']} {e['created_at'][:16].replace('T', ' ')}Z {e['name']}"
              f"{' (handled)' if e['handled'] else ''}: {json.dumps(e['payload'], ensure_ascii=False)}")


def cmd_handled(args):
    r = api("POST", f"/artifacts/{resolve(args.ref)}/events/handled", {"upto": args.upto})
    print(f"Marked {r['handled']} event(s) handled.")


def cmd_versions(args):
    r = api("GET", f"/artifacts/{resolve(args.ref)}/versions")
    print(f"current: v{r['current']}")
    for v in r["versions"]:
        print(f"v{v['version']}  {v['created_at'][:16].replace('T', ' ')}Z  {v['size']} bytes  {v.get('note') or ''}")


def cmd_restore(args):
    r = api("POST", f"/artifacts/{resolve(args.ref)}/restore", {"version": args.version})
    print(f"Restored v{args.version} as v{r['version']}.")


def cmd_archive(args):
    api("PATCH", f"/artifacts/{resolve(args.ref)}", {"archived": True})
    print("Archived.")


def cmd_delete(args):
    aid = resolve(args.ref)
    a = api("GET", f"/artifacts/{aid}")
    if not args.yes:
        decide(f"CONFIRM: deleting {a['title']!r} removes the page AND everything it stored, for good. "
               "Ask the user; repeat with --yes if they confirm. (Archive keeps it.)")
    api("DELETE", f"/artifacts/{aid}")
    print(f"Deleted {a['title']!r}.")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, *positional):
        sp = sub.add_parser(name)
        for pos in positional:
            sp.add_argument(pos)
        sp.set_defaults(fn=fn)
        return sp

    sp = add("list", cmd_list); sp.add_argument("--q"); sp.add_argument("--all", action="store_true")
    add("show", cmd_show, "ref")
    sp = add("publish", cmd_publish)
    sp.add_argument("--title", required=True); sp.add_argument("--html-file", required=True)
    for flag in ("--description", "--data-notes", "--crew", "--alert", "--session", "--state-file"):
        sp.add_argument(flag)
    sp.add_argument("--collection", action="append")
    sp = add("update", cmd_update, "ref")
    sp.add_argument("--html-file")
    for flag in ("--title", "--description", "--data-notes", "--note"):
        sp.add_argument(flag)
    sp = add("html", cmd_html, "ref"); sp.add_argument("--out", required=True); sp.add_argument("--version", type=int)
    add("data", cmd_data, "ref")
    add("state", cmd_state, "ref")
    add_json_args(add("state-set", cmd_state_set, "ref"))
    add_json_args(add("state-patch", cmd_state_patch, "ref"))
    add("docs", cmd_docs, "ref", "collection")
    sp = add("add", cmd_add, "ref", "collection"); add_json_args(sp); sp.add_argument("--id")
    sp = add("patch", cmd_patch, "ref", "collection", "id"); add_json_args(sp)
    add("remove", cmd_remove, "ref", "collection", "id")
    add("clear", cmd_clear, "ref", "collection")
    sp = add("events", cmd_events, "ref"); sp.add_argument("--unhandled", action="store_true")
    sp.add_argument("--after", type=int, default=0)
    sp = add("handled", cmd_handled, "ref", "upto"); sp.set_defaults(fn=cmd_handled)
    add("versions", cmd_versions, "ref")
    sp = add("restore", cmd_restore, "ref", "version")
    add("archive", cmd_archive, "ref")
    sp = add("delete", cmd_delete, "ref"); sp.add_argument("--yes", action="store_true")

    args = p.parse_args()
    for numeric in ("upto", "version"):
        if hasattr(args, numeric) and isinstance(getattr(args, numeric), str):
            try:
                setattr(args, numeric, int(getattr(args, numeric)))
            except ValueError:
                die(f"{numeric} must be a number")
    args.fn(args)


if __name__ == "__main__":
    main()
