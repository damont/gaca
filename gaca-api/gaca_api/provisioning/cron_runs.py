"""Read a Hermes profile's scheduled-run history off disk.

Hermes keeps one row per run in `<profile>/cron/executions.db` and the text of each run in
`<profile>/cron/output/<job_id>/<finish time>.md`. Its API serves neither (the dashboard's
`/api/cron/jobs/{id}/runs` is loopback-only and logged in), so gaca-api reads them, the way it
reads a profile's SOUL.md: read-only, size-capped, never following a symlink.

This file is standard library only and is used two ways, from one copy of the logic: imported by
the local provisioner, and sent whole to the box by the SSH provisioner (`script()` below), so the
two can't drift apart.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import sys
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

JOB_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
PROFILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
OUTPUT_TAIL = 6000      # bytes of a run's output kept: its end, which is where the response is
MAX_LIMIT = 100
MAX_ROWS = 600
_COLUMNS = "id, job_id, status, claimed_at, started_at, finished_at, error, delivery_outcome"


def _regular(path: Path) -> bool:
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def _tail(path: Path, size: int) -> str | None:
    """The last `size` bytes of a regular, non-symlink file as text, else None."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        end = os.fstat(fd).st_size
        os.lseek(fd, max(0, end - size), os.SEEK_SET)
        return os.read(fd, size).decode("utf-8", errors="replace")
    except OSError:
        return None
    finally:
        os.close(fd)


def _output_for(directory: Path, finished_at: str | None) -> str | None:
    """The run's output file. Named for the second the run finished (local time), so look for that
    and the seconds either side, in case the clock ticked between the ledger and the file."""
    if not finished_at or not directory.is_dir() or directory.is_symlink():
        return None
    try:
        when = datetime.fromisoformat(finished_at)
    except ValueError:
        return None
    for shift in (0, -1, 1):
        stamp = (when + timedelta(seconds=shift)).strftime("%Y-%m-%d_%H-%M-%S")
        path = directory / f"{stamp}.md"
        if _regular(path):
            return _tail(path, OUTPUT_TAIL)
    return None


def read(profile_dir: Path, job_id: str | None, limit: int, with_output: bool) -> list[dict]:
    """Newest-first execution rows. With `job_id`, that job's last `limit`; else every job's last `limit`."""
    if job_id is not None and not JOB_ID.fullmatch(job_id):
        raise ValueError(f"not a valid job id: {job_id!r}")
    limit = max(1, min(int(limit), MAX_LIMIT))
    cron = profile_dir / "cron"
    db = cron / "executions.db"
    if cron.is_symlink() or not _regular(db):
        return []
    # Opened read-only: this must never create, lock out or change Hermes' own ledger.
    conn = sqlite3.connect(f"file:{quote(str(db))}?mode=ro", uri=True, timeout=2.0)
    conn.row_factory = sqlite3.Row
    try:
        if job_id is not None:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM executions WHERE job_id = ? "
                "ORDER BY julianday(claimed_at) DESC, claimed_at DESC, id DESC LIMIT ?",
                (job_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY job_id "
                "ORDER BY julianday(claimed_at) DESC, claimed_at DESC, id DESC) AS n FROM executions) "
                "WHERE n <= ? ORDER BY julianday(claimed_at) DESC, claimed_at DESC, id DESC LIMIT ?",
                (limit, MAX_ROWS),
            ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    out = [dict(row) for row in rows]
    if with_output:
        for row in out:
            if JOB_ID.fullmatch(str(row["job_id"])):
                row["output"] = _output_for(cron / "output" / row["job_id"], row.get("finished_at"))
    return out


def main(argv: list[str]) -> None:
    """On the box: `python3 -c <this file + main(sys.argv[1:])> <profile> <job id or -> <limit> <0|1>`."""
    name, job, limit, with_output = argv[0], argv[1], int(argv[2]), argv[3] == "1"
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
    rows = read(directory, None if job == "-" else job, limit, with_output)
    print(json.dumps({"found": True, "rows": rows}))


def script() -> str:
    """This file plus a call to main(), for `python3 -c` on the box."""
    return Path(__file__).read_text(encoding="utf-8") + "\nmain(sys.argv[1:])\n"
