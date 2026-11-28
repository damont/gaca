"""Read how many model tokens a Hermes profile used, off disk.

Hermes records every session (a chat, a scheduled run) in `<profile>/state.db` with its token
counts: `input_tokens` (prompt tokens paid in full), `cache_read_tokens` (prompt tokens served
from the provider's cache), `output_tokens`, and how many model calls it made. Its API does not
serve totals, so gaca-api reads them, the way it reads the cron run ledger (cron_runs.py):
read-only, never following a symlink, and never able to lock out or change Hermes' own files.

Alongside the totals it reads the profile's scheduled jobs (`cron/jobs.json`), so a finding can
say what a job is set up to do (its schedule, whether it already has a pre-run script).

Standard library only, used two ways from one copy: imported by the local provisioner, and sent
whole to the box by the SSH provisioner (`script()`), so the two can't drift apart.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import sys
from pathlib import Path
from urllib.parse import quote

PROFILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_CRON_ID = re.compile(r"cron_([A-Za-z0-9_-]{1,64}?)_\d{8}_\d{6}")
_JOBS_LIMIT = 4 * 1024 * 1024
_MAX_SESSIONS = 20000
_JOB_FIELDS = ("id", "name", "schedule_display", "script", "monitor_script", "monitor_url", "no_agent",
               "model", "enabled", "skills")


def _regular(path: Path) -> bool:
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def _connect(db: Path) -> sqlite3.Connection:
    """Read-only. A WAL database normally needs its -shm file; when the box (or the sandbox) won't
    let us use it, fall back to reading the main file as immutable, which may miss the last few
    minutes that are still in the WAL. Good enough for a weekly total."""
    conn = sqlite3.connect(f"file:{quote(str(db))}?mode=ro", uri=True, timeout=2.0)
    try:
        conn.execute("SELECT 1 FROM sessions LIMIT 1").fetchall()
        return conn
    except sqlite3.Error:
        conn.close()
    return sqlite3.connect(f"file:{quote(str(db))}?mode=ro&immutable=1", uri=True, timeout=2.0)


def _job_key(session_id: str, source: str) -> tuple[str, str | None]:
    """`("job", id)` for a scheduled run (Hermes names them cron_<job id>_<date>_<time>), else
    `("chat", None)` grouped by where it came from."""
    if source == "cron":
        match = _CRON_ID.fullmatch(session_id or "")
        if match:
            return "job", match.group(1)
    return "chat", None


def _sessions(db: Path, since: float, until: float) -> list[dict]:
    conn = _connect(db)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT s.id, s.source, s.title, s.model, s.api_call_count, s.input_tokens, s.output_tokens, "
            "s.cache_read_tokens, s.cache_write_tokens, s.reasoning_tokens, "
            "(SELECT m.content FROM messages m WHERE m.session_id = s.id AND m.role = 'assistant' "
            " AND COALESCE(m.content, '') <> '' ORDER BY m.id DESC LIMIT 1) AS last_reply "
            "FROM sessions s WHERE s.started_at >= ? AND s.started_at < ? LIMIT ?",
            (since, until, _MAX_SESSIONS),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _silent(reply: object) -> bool:
    if not isinstance(reply, str):
        return False
    text = reply.strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return text == "[SILENT]" or bool(lines) and (lines[0] == "[SILENT]" or lines[-1] == "[SILENT]")


def _num(value: object) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def summarize(rows: list[dict]) -> list[dict]:
    """One entry per scheduled job (by id) and per chat source, with summed counts."""
    groups: dict[str, dict] = {}
    for row in rows:
        kind, job_id = _job_key(str(row.get("id") or ""), str(row.get("source") or ""))
        key = f"job:{job_id}" if kind == "job" else f"chat:{row.get('source') or 'unknown'}"
        group = groups.setdefault(key, {
            "key": key, "kind": kind, "job_id": job_id, "source": row.get("source") or "unknown", "name": None,
            "runs": 0, "silent_runs": 0, "calls": 0, "input_tokens": 0, "cache_read_tokens": 0,
            "cache_write_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0, "models": {},
        })
        title = str(row.get("title") or "")
        if kind == "job" and title and group["name"] is None:
            group["name"] = title.split(" · ")[0].strip()[:120] or None
        group["runs"] += 1
        group["silent_runs"] += 1 if _silent(row.get("last_reply")) else 0
        group["calls"] += _num(row.get("api_call_count"))
        for field in ("input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens", "reasoning_tokens"):
            group[field] += _num(row.get(field))
        model = str(row.get("model") or "")[:80]
        if model:
            group["models"][model] = group["models"].get(model, 0) + 1
    out = []
    for group in groups.values():
        models = group.pop("models")
        group["model"] = max(models, key=models.get) if models else None
        out.append(group)
    return out


def _jobs(profile_dir: Path) -> list[dict]:
    cron = profile_dir / "cron"
    path = cron / "jobs.json"
    if cron.is_symlink() or not _regular(path):
        return []
    try:
        if os.lstat(path).st_size > _JOBS_LIMIT:
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    jobs = data.get("jobs", []) if isinstance(data, dict) else data
    out = []
    for job in jobs if isinstance(jobs, list) else []:
        if not isinstance(job, dict) or not job.get("id"):
            continue
        item = {k: job.get(k) for k in _JOB_FIELDS}
        if item.get("schedule_display") is None and isinstance(job.get("schedule"), dict):
            item["schedule_display"] = job["schedule"].get("display") or job["schedule"].get("expr")
        for k in ("id", "name", "schedule_display", "script", "monitor_script", "monitor_url", "model"):
            item[k] = str(item[k])[:200] if item[k] is not None else None
        item["skills"] = [str(s)[:80] for s in item["skills"]][:20] if isinstance(item["skills"], list) else []
        item["enabled"] = item["enabled"] is not False
        item["no_agent"] = bool(item["no_agent"])
        out.append(item)
    return out


def read(profile_dir: Path, since: float, until: float) -> dict:
    """`{"groups": [...], "jobs": [...]}` for sessions started in [since, until) (epoch seconds)."""
    db = profile_dir / "state.db"
    groups = summarize(_sessions(db, float(since), float(until))) if _regular(db) else []
    return {"groups": groups, "jobs": _jobs(profile_dir)}


def main(argv: list[str]) -> None:
    """On the box: `python3 -c <this file + main(sys.argv[1:])> <profile> <since> <until>`."""
    name, since, until = argv[0], float(argv[1]), float(argv[2])
    home = Path.home() / ".hermes"
    if name == "default":
        directory = home
    elif PROFILE.fullmatch(name):
        directory = home / "profiles" / name
    else:
        directory = None
    if directory is None or not directory.is_dir() or directory.is_symlink():
        print(json.dumps({"found": False}))
        return
    print(json.dumps({"found": True, **read(directory, since, until)}))


def script() -> str:
    """This file plus a call to main(), for `python3 -c` on the box."""
    return Path(__file__).read_text(encoding="utf-8") + "\nmain(sys.argv[1:])\n"
