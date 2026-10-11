"""A phone's push token belongs to the paired device that registered it, so unpairing the phone, by any
route, stops its alerts: the app disconnecting, `gaca-api device revoke`, a lost or stolen phone."""
import asyncio
import json
import sqlite3
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from gaca_api import cli
from gaca_api.config import settings
from gaca_api.db import client as db_client
from gaca_api.main import app
from gaca_api.models.base import utcnow
from gaca_api.services import pairing

CODE = "ABCD2345EFGH6789"


@pytest.fixture
def http(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "t.db"))
    monkeypatch.setattr(settings, "agent_engine", "hermes")
    with TestClient(app) as c:
        c.db = str(tmp_path / "t.db")
        yield c


def pair(http, name):
    state = {"hash": pairing._digest(CODE), "expires_at": (utcnow() + timedelta(minutes=10)).isoformat(), "wrong": 0}
    con = sqlite3.connect(http.db)
    con.execute("INSERT OR REPLACE INTO server_meta (key, value) VALUES ('pair_code', ?)", (json.dumps(state),))
    con.commit()
    con.close()
    made = http.post("/api/devices/pair", json={"code": CODE, "name": name}).json()
    return {"id": made["device_id"], "h": {"Authorization": "Bearer " + made["access_token"]}}


def register(http, device, token, platform="android"):
    r = http.post("/api/push/register", headers=device["h"], json={"token": token, "platform": platform})
    assert r.status_code == 204, r.text


def tokens(http):
    con = sqlite3.connect(http.db)
    try:
        return dict(con.execute("SELECT token, device_id FROM push_tokens").fetchall())
    finally:
        con.close()


def test_a_registered_token_is_linked_to_the_device_that_registered_it(http):
    phone = pair(http, "Pixel")
    register(http, phone, "fcm-A")
    assert tokens(http) == {"fcm-A": phone["id"]}


def test_disconnecting_removes_the_token_even_if_the_app_never_unregistered(http):
    phone, tablet = pair(http, "Pixel"), pair(http, "Tablet")
    register(http, phone, "fcm-phone")
    register(http, tablet, "fcm-tablet")
    assert http.delete("/api/devices/me", headers=phone["h"]).status_code == 204  # no /push/unregister before it
    assert tokens(http) == {"fcm-tablet": tablet["id"]}  # only the leaving phone's token went


def test_revoking_a_lost_phone_from_the_server_stops_its_alerts(http):
    phone, tablet = pair(http, "Pixel"), pair(http, "Tablet")
    register(http, phone, "fcm-lost-phone")
    register(http, tablet, "fcm-tablet")
    assert http.delete(f"/api/devices/{phone['id']}", headers=tablet["h"]).status_code == 204
    assert tokens(http) == {"fcm-tablet": tablet["id"]}


def test_the_cli_revoke_does_the_same(http, tmp_path):
    phone = pair(http, "Pixel")
    register(http, phone, "fcm-A")
    http.__exit__(None, None, None)  # let the CLI have the database to itself
    assert cli.main(["device", "revoke", phone["id"]]) == 0
    con = sqlite3.connect(http.db)
    assert con.execute("SELECT COUNT(*) FROM push_tokens").fetchone()[0] == 0
    con.close()


def test_a_token_that_moves_to_another_device_follows_it(http):
    a, b = pair(http, "A"), pair(http, "B")
    register(http, a, "fcm-shared")
    register(http, b, "fcm-shared")  # same physical phone, now paired as B
    assert tokens(http) == {"fcm-shared": b["id"]}
    assert http.delete(f"/api/devices/{a['id']}", headers=b["h"]).status_code == 204
    assert tokens(http) == {"fcm-shared": b["id"]}  # revoking the old pairing does not touch it


def test_an_iphone_token_is_handled_the_same_way(http):
    phone = pair(http, "iPhone")
    register(http, phone, "apns-A", platform="ios")
    http.delete("/api/devices/me", headers=phone["h"])
    assert tokens(http) == {}


def test_tokens_registered_before_this_existed_stay_until_the_app_registers_again(http):
    phone = pair(http, "Pixel")
    con = sqlite3.connect(http.db)
    owner_id = con.execute("SELECT id FROM users").fetchone()[0]
    con.execute("INSERT INTO push_tokens (id, user_id, token, created_at, doc, device_id) VALUES ('x', ?, 'old', '2026-01-01', ?, NULL)",
                (owner_id, json.dumps({"id": "x", "user_id": owner_id, "token": "old", "platform": "android", "created_at": "2026-01-01T00:00:00+00:00"})))
    con.commit()
    con.close()
    http.delete("/api/devices/me", headers=phone["h"])
    assert tokens(http) == {"old": None}  # nothing links it to a device yet
    phone2 = pair(http, "Pixel again")
    register(http, phone2, "old")  # the app's next launch re-registers: now it is linked
    assert tokens(http) == {"old": phone2["id"]}
    http.delete("/api/devices/me", headers=phone2["h"])
    assert tokens(http) == {}


def test_explicit_unregister_still_works(http):
    phone = pair(http, "Pixel")
    register(http, phone, "fcm-A")
    assert http.post("/api/push/unregister", headers=phone["h"], json={"token": "fcm-A"}).status_code == 204
    assert tokens(http) == {}


def test_an_existing_database_gets_the_new_column(tmp_path, monkeypatch):
    db = tmp_path / "old.db"
    monkeypatch.setattr(settings, "db_path", str(db))
    legacy = sqlite3.connect(db)
    legacy.execute("CREATE TABLE push_tokens (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, token TEXT NOT NULL, created_at TEXT NOT NULL, doc TEXT NOT NULL)")
    legacy.execute("INSERT INTO push_tokens VALUES ('1','u','t','2026-01-01','{}')")
    legacy.commit()
    legacy.close()

    async def open_and_close():
        await db_client.connect()
        await db_client.close()
    asyncio.run(open_and_close())
    con = sqlite3.connect(db)
    assert "device_id" in {r[1] for r in con.execute("PRAGMA table_info(push_tokens)")}
    assert con.execute("SELECT device_id FROM push_tokens").fetchone() == (None,)
    con.close()


def test_only_a_real_paired_device_has_a_device_id(tmp_path, monkeypatch):
    from gaca_api.db import api_tokens as tokens_db
    from gaca_api.models.api_token import ApiTokenDoc
    from gaca_api.utils import tokens as token_utils
    from gaca_api.utils.auth import device_id_for
    from conftest import make_owner

    monkeypatch.setattr(settings, "db_path", str(tmp_path / "t.db"))

    async def go():
        await db_client.connect()
        try:
            owner = await make_owner()
            # a row the API would never mint: a device-looking secret behind a software scope
            await tokens_db.insert(ApiTokenDoc(user_id=owner.id, name="odd", scopes=["alerts"]), token_utils.digest("gaca_dev_planted"))
            code, _ = await pairing.issue_code()
            real, secret = await pairing.pair(code, "Pixel")
            return (await device_id_for("gaca_dev_planted"), await device_id_for(secret), real.id,
                    await device_id_for("gaca_pat_whatever"), await device_id_for("garbage"))
        finally:
            await db_client.close()
    odd, real, real_id, pat, garbage = asyncio.run(go())
    assert odd is None and real == real_id and pat is None and garbage is None
