"""Pairing: how a phone becomes allowed to use the server. The code is printed on the box
(`gaca-api pair`) and traded for a revocable device credential — no passwords.

Codes are planted straight into the database file, hashed exactly as the real thing stores
them, because the server never hands one out over HTTP.
"""
import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from gaca_api import cli
from gaca_api.config import settings
from gaca_api.db import client as db_client
from gaca_api.main import app
from gaca_api.models.base import utcnow
from gaca_api.services import pairing
from gaca_api.utils import tokens as token_utils

CODE = "ABCD2345EFGH6789"


@pytest.fixture
def http(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "t.db"))
    monkeypatch.setattr(settings, "agent_engine", "hermes")
    with TestClient(app) as c:
        c.db = str(tmp_path / "t.db")
        yield c


def plant(db, raw=CODE, minutes=10, wrong=0):
    state = {"hash": pairing._digest(raw), "expires_at": (utcnow() + timedelta(minutes=minutes)).isoformat(), "wrong": wrong}
    con = sqlite3.connect(db)
    con.execute("INSERT OR REPLACE INTO server_meta (key, value) VALUES ('pair_code', ?)", (json.dumps(state),))
    con.commit()
    con.close()


def rows(db, sql, *args):
    con = sqlite3.connect(db)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def pair(http, code=CODE, name="Pixel 7"):
    return http.post("/api/devices/pair", json={"code": code, "name": name})


def bearer(secret):
    return {"Authorization": f"Bearer {secret}"}


def paired(http, name="Pixel 7"):
    plant(http.db)
    r = pair(http, name=name)
    assert r.status_code == 201, r.text
    return r.json()


# ── pairing ──────────────────────────────────────────────────────────

def test_first_pairing_creates_the_owner_and_returns_a_device_credential(http):
    assert http.get("/api/setup").json()["claimed"] is False
    made = paired(http)
    assert made["access_token"].startswith(token_utils.DEVICE_PREFIX) and made["device_id"]
    assert http.get("/api/setup").json()["claimed"] is True
    assert http.get("/api/auth/me", headers=bearer(made["access_token"])).status_code == 200
    assert len(rows(http.db, "SELECT id FROM users")) == 1


def test_a_second_device_joins_the_same_owner(http):
    a, b = paired(http, "Pixel 7"), paired(http, "Tablet")
    assert a["access_token"] != b["access_token"]
    assert len(rows(http.db, "SELECT id FROM users")) == 1
    listed = http.get("/api/devices", headers=bearer(a["access_token"])).json()
    assert {d["name"] for d in listed} == {"Pixel 7", "Tablet"}
    assert [d["name"] for d in listed if d["current"]] == ["Pixel 7"]


def test_the_credential_is_stored_only_as_a_hash(http):
    made = paired(http)
    stored = rows(http.db, "SELECT token_hash FROM api_tokens")[0][0]
    assert stored == token_utils.digest(made["access_token"])
    assert made["access_token"].encode() not in open(http.db, "rb").read()


def test_a_code_works_once(http):
    plant(http.db)
    assert pair(http).status_code == 201
    again = pair(http)
    assert again.status_code == 403 and "pair" in again.json()["detail"]
    assert rows(http.db, "SELECT COUNT(*) FROM api_tokens")[0][0] == 1


def test_wrong_code_is_refused_and_creates_nothing(http):
    plant(http.db)
    assert pair(http, code="WRONGWRONGWRONG12").status_code == 401
    assert rows(http.db, "SELECT COUNT(*) FROM api_tokens")[0][0] == 0
    assert rows(http.db, "SELECT COUNT(*) FROM users")[0][0] == 0


def test_no_code_active_is_refused(http):
    assert pair(http).status_code == 403


def test_expired_code_is_refused(http):
    plant(http.db, minutes=-1)
    assert pair(http).status_code == 403
    assert rows(http.db, "SELECT COUNT(*) FROM server_meta WHERE key = 'pair_code'")[0][0] == 0


def test_code_is_burned_after_too_many_wrong_guesses(http, monkeypatch):
    monkeypatch.setattr(settings, "auth_attempts_per_minute", 1000)
    plant(http.db)
    for _ in range(pairing.MAX_WRONG_ATTEMPTS - 1):
        assert pair(http, code="NOPENOPENOPE2345").status_code == 401
    assert pair(http, code="NOPENOPENOPE2345").status_code == 403
    assert pair(http).status_code == 403  # even the right one, now


def test_the_code_tolerates_dashes_case_and_spaces(http):
    plant(http.db)
    assert pair(http, code="abcd-2345 efgh-6789").status_code == 201


def test_simultaneous_pairings_with_one_code_yield_one_device(http):
    plant(http.db)
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda i: pair(http, name=f"d{i}").status_code, range(8)))
    assert results.count(201) == 1
    assert rows(http.db, "SELECT COUNT(*) FROM api_tokens")[0][0] == 1


def test_pairing_is_rate_limited(http, monkeypatch):
    monkeypatch.setattr(settings, "auth_attempts_per_minute", 3)
    codes = [pair(http, code="NOPENOPENOPE2345").status_code for _ in range(5)]
    assert codes[:3] == [403] * 3 and codes[3:] == [429, 429]


def test_the_device_name_is_trimmed_and_defaulted(http):
    plant(http.db)
    assert pair(http, name="  " + "x" * 200).status_code == 422  # over the cap: refused, not truncated silently
    plant(http.db)
    assert pair(http, name="   ").status_code == 201
    assert rows(http.db, "SELECT name FROM api_tokens")[0][0] == "Phone"


# ── what a device can and cannot do ──────────────────────────────────

def test_a_device_has_the_owners_full_access_including_managing_tokens(http):
    made = paired(http)
    h = bearer(made["access_token"])
    assert http.get("/api/crew", headers=h).status_code == 200
    minted = http.post("/api/tokens", headers=h, json={"name": "hermes", "scopes": ["alerts"]})
    assert minted.status_code == 201
    listed = http.get("/api/tokens", headers=h).json()
    assert [t["name"] for t in listed] == ["hermes"]  # devices are not in the software-token list


def test_a_hermes_token_cannot_touch_devices(http):
    made = paired(http)
    secret = http.post("/api/tokens", headers=bearer(made["access_token"]), json={"name": "h", "scopes": ["alerts", "lists", "briefings", "artifacts"]}).json()["token"]
    assert http.get("/api/devices", headers=bearer(secret)).status_code == 403
    assert http.delete(f"/api/devices/{made['device_id']}", headers=bearer(secret)).status_code == 403
    assert http.delete("/api/devices/me", headers=bearer(secret)).status_code == 403


def test_a_device_token_with_a_tampered_prefix_is_not_accepted_as_a_hermes_token(http):
    made = paired(http)
    forged = token_utils.PREFIX + made["access_token"][len(token_utils.DEVICE_PREFIX):]
    assert http.get("/api/auth/me", headers=bearer(forged)).status_code == 401


def test_a_hermes_token_cannot_be_used_as_a_device(http):
    made = paired(http)
    secret = http.post("/api/tokens", headers=bearer(made["access_token"]), json={"name": "h", "scopes": ["alerts"]}).json()["token"]
    forged = token_utils.DEVICE_PREFIX + secret[len(token_utils.PREFIX):]
    assert http.get("/api/crew", headers=bearer(forged)).status_code == 401


def _plant_credential(http, secret, scopes):
    """A row the API itself would never mint: a credential whose prefix and scope disagree."""
    owner_id = rows(http.db, "SELECT id FROM users")[0][0]
    con = sqlite3.connect(http.db)
    con.execute(
        "INSERT INTO api_tokens (id, user_id, name, token_hash, scopes, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (token_utils.digest(secret)[:16], owner_id, "planted", token_utils.digest(secret), json.dumps(scopes), utcnow().isoformat()),
    )
    con.commit()
    con.close()


def test_the_prefix_and_the_scope_must_agree_even_if_a_row_says_otherwise(http):
    paired(http)
    as_pat = token_utils.PREFIX + "planted-device-scope"
    _plant_credential(http, as_pat, [token_utils.DEVICE_SCOPE])
    assert http.get("/api/auth/me", headers=bearer(as_pat)).status_code == 401  # a device scope behind a pat prefix
    as_dev = token_utils.DEVICE_PREFIX + "planted-alerts-scope"
    _plant_credential(http, as_dev, ["alerts"])
    assert http.get("/api/crew", headers=bearer(as_dev)).status_code == 401  # a software scope behind a device prefix


def test_unknown_devices_are_refused(http):
    assert http.get("/api/crew", headers=bearer(token_utils.DEVICE_PREFIX + "nope")).status_code == 401


# ── unpairing ────────────────────────────────────────────────────────

def test_revoking_a_device_cuts_it_off_and_leaves_the_others(http):
    a, b = paired(http, "A"), paired(http, "B")
    assert http.delete(f"/api/devices/{b['device_id']}", headers=bearer(a["access_token"])).status_code == 204
    assert http.get("/api/crew", headers=bearer(b["access_token"])).status_code == 401
    assert http.get("/api/crew", headers=bearer(a["access_token"])).status_code == 200
    assert [d["name"] for d in http.get("/api/devices", headers=bearer(a["access_token"])).json()] == ["A"]


def test_a_device_can_unpair_itself(http):
    a, b = paired(http, "A"), paired(http, "B")
    assert http.delete("/api/devices/me", headers=bearer(a["access_token"])).status_code == 204
    assert http.get("/api/crew", headers=bearer(a["access_token"])).status_code == 401
    assert http.get("/api/crew", headers=bearer(b["access_token"])).status_code == 200


def test_revoking_an_unknown_device_is_404(http):
    made = paired(http)
    assert http.delete("/api/devices/nope", headers=bearer(made["access_token"])).status_code == 404


def test_a_hermes_token_id_is_not_a_device_id(http):
    made = paired(http)
    h = bearer(made["access_token"])
    tid = http.post("/api/tokens", headers=h, json={"name": "h", "scopes": ["alerts"]}).json()["id"]
    assert http.delete(f"/api/devices/{tid}", headers=h).status_code == 404


def test_devices_need_a_credential(http):
    assert http.get("/api/devices").status_code == 401


# ── the owner tools ──────────────────────────────────────────────────

@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "gaca.db"))

    async def init():
        await db_client.connect()
        await db_client.close()
    asyncio.run(init())
    return tmp_path / "gaca.db"


def test_pair_prints_a_code_and_a_link_that_carries_the_address(server, capsys):
    assert cli.main(["pair", "--url", "https://gaca.example.com/", "--no-qr"]) == 0
    out = capsys.readouterr().out
    code = out.split("Pairing code:")[1].split()[0]
    assert "gaca://setup?url=https%3A%2F%2Fgaca.example.com&code=" + code.replace("-", "") in out

    async def trade():
        await db_client.connect()
        try:
            return await pairing.pair(code, "Phone")
        finally:
            await db_client.close()
    doc, secret = asyncio.run(trade())
    assert secret.startswith(token_utils.DEVICE_PREFIX) and doc.name == "Phone"


def test_pair_uses_the_configured_public_url_and_draws_a_qr(server, capsys, monkeypatch):
    monkeypatch.setattr(settings, "public_url", "https://gaca.example.com")
    assert cli.main(["pair"]) == 0
    out = capsys.readouterr().out
    assert "scan" in out and "gaca://setup?url=https%3A%2F%2Fgaca.example.com" in out
    assert "█" in out or "▀" in out or "▄" in out  # the QR code itself


def test_pair_without_an_address_says_how_to_continue(server, capsys, monkeypatch):
    monkeypatch.setattr(settings, "public_url", "")
    assert cli.main(["pair"]) == 0
    captured = capsys.readouterr()
    assert "Pairing code:" in captured.out and "gaca://" not in captured.out
    assert "GACA_PUBLIC_URL" in captured.err


def test_device_list_and_revoke(server, capsys):
    async def make():
        await db_client.connect()
        try:
            code, _ = await pairing.issue_code()
            return (await pairing.pair(code, "Pixel 7"))[0].id
        finally:
            await db_client.close()
    device_id = asyncio.run(make())
    assert cli.main(["device", "list"]) == 0
    assert "Pixel 7" in capsys.readouterr().out
    assert cli.main(["device", "revoke", "nope"]) == 1
    assert cli.main(["device", "revoke", device_id]) == 0
    assert cli.main(["device", "list"]) == 0
    assert "No paired devices" in capsys.readouterr().out


def test_device_commands_on_an_ownerless_server_point_at_pair(server, capsys):
    assert cli.main(["device", "list"]) == 1
    assert "gaca-api pair" in capsys.readouterr().err
