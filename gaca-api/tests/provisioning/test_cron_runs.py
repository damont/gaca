"""Reading a Hermes profile's run history: one copy of the logic, used locally and on the box."""
import asyncio
import json
import os
import sqlite3
import subprocess
import sys

import pytest

from gaca_api.provisioning import cron_runs
from gaca_api.provisioning.local import LocalHermes

SCHEMA = """CREATE TABLE executions (id TEXT PRIMARY KEY, job_id TEXT NOT NULL, source TEXT NOT NULL,
  process_id TEXT NOT NULL, pid INTEGER NOT NULL, process_started_at INTEGER, status TEXT NOT NULL,
  handoff_pending INTEGER NOT NULL DEFAULT 0, handoff_started_at REAL, claimed_at TEXT NOT NULL,
  started_at TEXT, finished_at TEXT, error TEXT, delivery_outcome TEXT, scheduled_instant TEXT)"""


def _ledger(directory, rows):
    cron = directory / "cron"
    cron.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(cron / "executions.db")
    conn.execute(SCHEMA)
    for i, (job, status, day, error) in enumerate(rows):
        conn.execute(
            "INSERT INTO executions (id, job_id, source, process_id, pid, status, claimed_at, started_at, finished_at, error)"
            " VALUES (?, ?, 'builtin', 'p', 1, ?, ?, ?, ?, ?)",
            (f"e{i}", job, status, f"2026-10-{day}T07:00:00-04:00", f"2026-10-{day}T07:00:01-04:00",
             f"2026-10-{day}T07:00:41-04:00", error))
    conn.commit()
    conn.close()


def _output(directory, job, day, text):
    out = directory / "cron" / "output" / job
    out.mkdir(parents=True, exist_ok=True)
    (out / f"2026-10-{day}_07-00-41.md").write_text(text)


@pytest.fixture
def home(tmp_path):
    home = tmp_path / ".hermes"
    _ledger(home, [("aaaaaaaaaaaa", "completed", "16", None), ("aaaaaaaaaaaa", "completed", "17", None),
                   ("aaaaaaaaaaaa", "failed", "18", "boom"), ("bbbbbbbbbbbb", "completed", "18", None)])
    _output(home, "aaaaaaaaaaaa", "17", "# Cron Job\n## Prompt\nbig\n## Response\nwent fine")
    _output(home, "aaaaaaaaaaaa", "16", "x" * 20000 + "\n## Response\nthe end")
    _ledger(home / "profiles" / "toolshed", [("cccccccccccc", "completed", "18", None)])
    return home


def test_read_returns_newest_first_with_a_per_job_cap(home):
    everything = cron_runs.read(home, None, 2, False)
    assert [r["id"] for r in everything] == ["e3", "e2", "e1"]  # e0 is job a's third: capped away
    assert all("output" not in r for r in everything)
    one = cron_runs.read(home, "aaaaaaaaaaaa", 10, False)
    assert [r["id"] for r in one] == ["e2", "e1", "e0"]


def test_output_is_matched_by_finish_time_and_only_its_tail_is_kept(home):
    rows = {r["id"]: r for r in cron_runs.read(home, "aaaaaaaaaaaa", 10, True)}
    assert rows["e1"]["output"].endswith("went fine")
    assert rows["e0"]["output"].endswith("the end") and len(rows["e0"]["output"]) <= cron_runs.OUTPUT_TAIL
    assert rows["e2"]["output"] is None  # that run left no file


def test_nothing_to_read_is_an_empty_list_not_an_error(tmp_path):
    assert cron_runs.read(tmp_path, None, 5, True) == []
    (tmp_path / "cron").mkdir()
    (tmp_path / "cron" / "executions.db").write_text("not a database")
    assert cron_runs.read(tmp_path, None, 5, True) == []


def test_a_bad_job_id_is_refused_before_it_touches_a_path(home):
    for bad in ("../x", "a/b", "", "x" * 65, "a b"):
        with pytest.raises(ValueError):
            cron_runs.read(home, bad, 5, True)


def test_it_never_follows_a_symlink_or_writes(home, tmp_path):
    secret = tmp_path / "secret.md"
    secret.write_text("TOP SECRET")
    link = home / "cron" / "output" / "aaaaaaaaaaaa" / "2026-10-18_07-00-41.md"
    link.symlink_to(secret)
    rows = {r["id"]: r for r in cron_runs.read(home, "aaaaaaaaaaaa", 10, True)}
    assert rows["e2"]["output"] is None
    before = (home / "cron" / "executions.db").read_bytes()
    cron_runs.read(home, None, 5, True)
    assert (home / "cron" / "executions.db").read_bytes() == before


def test_local_provisioner(home):
    hermes = LocalHermes(home, "hermes")
    rows = asyncio.run(hermes.read_cron_runs("default", job_id="aaaaaaaaaaaa", limit=2, with_output=True))
    assert [r["id"] for r in rows] == ["e2", "e1"]
    assert [r["job_id"] for r in asyncio.run(hermes.read_cron_runs("toolshed", job_id=None, limit=5, with_output=False))] == ["cccccccccccc"]
    assert asyncio.run(hermes.read_cron_runs("ghost", job_id=None, limit=5, with_output=False)) is None
    for hostile in ("../x", "a/b", "$(id)"):
        with pytest.raises(Exception):
            asyncio.run(hermes.read_cron_runs(hostile, job_id=None, limit=5, with_output=False))
    with pytest.raises(Exception):
        asyncio.run(hermes.read_cron_runs("default", job_id="../x", limit=5, with_output=False))


def _on_box(tmp_path, *args):
    out = subprocess.run([sys.executable, "-c", cron_runs.script(), *args], env={**os.environ, "HOME": str(tmp_path)},
                         capture_output=True, text=True)
    return out


def test_the_script_sent_to_the_box_matches_the_local_reader(home, tmp_path):
    local = cron_runs.read(home, "aaaaaaaaaaaa", 10, True)
    box = json.loads(_on_box(tmp_path, "default", "aaaaaaaaaaaa", "10", "1").stdout)
    assert box == {"found": True, "rows": local}
    toolshed = json.loads(_on_box(tmp_path, "toolshed", "-", "5", "0").stdout)
    assert [r["job_id"] for r in toolshed["rows"]] == ["cccccccccccc"]
    for missing in ("ghost", "../x", "..", "a/b", "$(id)"):
        assert json.loads(_on_box(tmp_path, missing, "-", "5", "0").stdout) == {"found": False}, missing
    assert _on_box(tmp_path, "default", "../x", "5", "0").returncode != 0
