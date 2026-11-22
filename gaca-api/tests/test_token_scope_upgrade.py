"""Existing agent tokens get the `reminders` scope at the first start after it arrived, once.

The Hermes tokens already on a box were minted before reminders existed. Re-minting means rotating
secrets and restarting Hermes, so the API widens the ones that hold the old defaults, one time, and
leaves alone anything narrowed on purpose or revoked."""
import asyncio
import json
import sqlite3

from gaca_api.config import settings
from gaca_api.db import client

OLD = ["alerts", "briefings", "artifacts", "heartbeat"]


def seed(path, rows, clear_marker=True):
    con = sqlite3.connect(path)
    if clear_marker:
        con.execute("DELETE FROM server_meta WHERE key = 'migration:token-scope-reminders'")
    for i, (scopes, revoked) in enumerate(rows):
        con.execute(
            "INSERT INTO api_tokens (id, user_id, name, token_hash, scopes, created_at, revoked_at) VALUES (?, 'u', ?, ?, ?, '2026-01-01T00:00:00+00:00', ?)",
            (f"t{i}", f"tok{i}", f"h{i}", json.dumps(scopes), "2026-02-01T00:00:00+00:00" if revoked else None),
        )
    con.commit()
    con.close()


def scopes_of(path):
    con = sqlite3.connect(path)
    try:
        return {r[0]: json.loads(r[1]) for r in con.execute("SELECT id, scopes FROM api_tokens")}
    finally:
        con.close()


def restart(path):  # the database path is already set by `start`
    async def go():
        await client.connect()
        await client.close()
    asyncio.run(go())


def start(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(settings, "db_path", str(path))
    restart(path)  # a fresh install: schema, and the migration marked as done
    return path


def test_tokens_with_the_old_defaults_get_reminders_and_the_rest_are_left_alone(tmp_path, monkeypatch):
    path = start(tmp_path, monkeypatch)
    seed(path, [(OLD, False), ([*OLD, "cloud"], False), (["alerts"], False), (["alerts", "briefings", "artifacts"], False),
                (OLD, True), (["device"], False), ([*OLD, "reminders"], False)])
    restart(path)
    got = scopes_of(path)
    assert got["t0"] == [*OLD, "reminders"] and got["t1"] == [*OLD, "cloud", "reminders"]
    assert got["t2"] == ["alerts"] and got["t3"] == ["alerts", "briefings", "artifacts"]  # narrowed on purpose
    assert got["t4"] == OLD  # revoked: never brought back to life with more
    assert got["t5"] == ["device"] and got["t6"] == [*OLD, "reminders"]  # a phone; and one already done, not doubled


def test_it_runs_once_so_a_later_narrow_token_is_not_widened(tmp_path, monkeypatch):
    path = start(tmp_path, monkeypatch)
    seed(path, [(OLD, False)])
    restart(path)
    con = sqlite3.connect(path)
    con.execute("INSERT INTO api_tokens (id, user_id, name, token_hash, scopes, created_at) VALUES ('later', 'u', 'later', 'hl', ?, '2026-03-01T00:00:00+00:00')", (json.dumps(OLD),))
    con.commit()
    con.close()
    restart(path)  # another start
    assert scopes_of(path)["later"] == OLD and scopes_of(path)["t0"] == [*OLD, "reminders"]


def test_a_fresh_install_has_nothing_to_widen_and_marks_it_done(tmp_path, monkeypatch):
    path = start(tmp_path, monkeypatch)
    con = sqlite3.connect(path)
    row = con.execute("SELECT value FROM server_meta WHERE key = 'migration:token-scope-reminders'").fetchone()
    con.close()
    assert json.loads(row[0]) == {"widened": 0}
