#!/usr/bin/env python3
"""Small, dependency-free GACA API client for the david-alerts skill."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# GACA runs on the same box as Hermes; always talk to it locally, never via the public URL.
DEFAULT_BASE_URL = "http://127.0.0.1:41700"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131 Safari/537.36 Hermes-GACA/1.0"


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def base_url() -> str:
    value = os.environ.get("GACA_BASE_URL", DEFAULT_BASE_URL).strip().rstrip("/")
    parsed = urllib.parse.urlparse(value)
    local = parsed.hostname in ("localhost", "127.0.0.1", "::1")
    if parsed.scheme not in ("http", "https") or (parsed.scheme == "http" and not local):
        raise SystemExit("GACA_BASE_URL must be https (http is only allowed for localhost)")
    if not parsed.netloc or parsed.username or parsed.password:
        raise SystemExit("GACA_BASE_URL must be an origin without credentials")
    return value


def token() -> str:
    value = os.environ.get("GACA_API_TOKEN", "").strip()
    if not value:
        raise SystemExit("GACA_API_TOKEN is not configured")
    if "\n" in value or "\r" in value:
        raise SystemExit("GACA_API_TOKEN contains an invalid newline")
    return value


def active_agent_name() -> str:
    """Return the active Hermes profile name for GACA alert attribution."""
    explicit = os.environ.get("HERMES_PROFILE", "").strip()
    if explicit:
        return explicit
    hermes_home = Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser().resolve()
    default_home = Path("~/.hermes").expanduser().resolve()
    if hermes_home.parent == default_home / "profiles" and hermes_home.name:
        return hermes_home.name
    return "default"


def alert_payload() -> dict[str, object]:
    """Read an alert and ensure GACA knows which Hermes profile sent it."""
    payload = stdin_json()
    if not isinstance(payload, dict):
        raise SystemExit("Alert JSON must be an object")
    agent = payload.get("agent")
    if agent is None or (isinstance(agent, str) and not agent.strip()):
        payload["agent"] = active_agent_name()
    # GACA's field for "which crew member posted this" is `crew` (it opens that crew member's chat when
    # the alert is taken to one). `agent` was this helper's older name for the same thing.
    if not str(payload.get("crew") or "").strip():
        payload["crew"] = str(payload["agent"]).strip()
    return payload


def call(method: str, path: str, payload: object | None = None, *, auth: bool = True) -> tuple[int, object]:
    url = f"{base_url()}{path}"
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    data = None
    if auth:
        headers["Authorization"] = f"Bearer {token()}"
    if payload is not None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    opener = urllib.request.build_opener(NoRedirects)
    try:
        with opener.open(request, timeout=30) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as error:
        raw = error.read()
        status = error.code
    except urllib.error.URLError as error:
        raise SystemExit(f"Network error contacting GACA: {error.reason}") from error

    text = raw.decode("utf-8", errors="replace")
    try:
        body: object = json.loads(text) if text else None
    except json.JSONDecodeError:
        body = {"raw_response": text[:4000]}

    if status >= 400:
        print(json.dumps({"http_status": status, "error": body}, indent=2), file=sys.stderr)
        raise SystemExit(2)
    return status, body


def stdin_json() -> object:
    if sys.stdin.isatty():
        raise SystemExit("Read alert JSON from standard input; do not put secrets in it")
    try:
        return json.load(sys.stdin)
    except json.JSONDecodeError as error:
        raise SystemExit(f"Invalid JSON on standard input: {error}") from error


def main() -> None:
    parser = argparse.ArgumentParser(description="Call the GACA alert API safely")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("schema", help="fetch the live OpenAPI schema")
    subparsers.add_parser("me", help="verify authentication and show current account")
    subparsers.add_parser("post", help="post alert JSON read from standard input")
    get_parser = subparsers.add_parser("get", help="read one alert by ID")
    get_parser.add_argument("alert_id")
    subparsers.add_parser("list", help="list recent alerts")
    args = parser.parse_args()

    if args.command == "schema":
        status, body = call("GET", "/api/openapi.json", auth=False)
    elif args.command == "me":
        status, body = call("GET", "/api/auth/me")
    elif args.command == "post":
        status, body = call("POST", "/api/alerts", alert_payload())
    elif args.command == "get":
        alert_id = urllib.parse.quote(args.alert_id, safe="")
        status, body = call("GET", f"/api/alerts/{alert_id}")
    else:
        status, body = call("GET", "/api/alerts?limit=50")

    print(json.dumps({"http_status": status, "response": body}, indent=2))


if __name__ == "__main__":
    main()
