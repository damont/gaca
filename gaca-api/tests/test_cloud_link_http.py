"""Linking this server to a GACA Cloud account: the device-code flow as a client, the token kept
encrypted, the status that never shows it, and unlinking (also with the cloud unreachable)."""
import json
import logging
import os
import sqlite3
import stat

import pytest

from gaca_api.config import settings
from gaca_api.services import cloud_client, cloud_crypto
from tests.cloud_support import (  # noqa: F401  (fixtures)
    CLOUD_URL, DEVICE_CODE, SERVER_TOKEN, _skip_interval, cloud, gacabit, http, link, mint, raw_db_bytes, script_link,
)


def start(http, **body):
    return http.post("/api/cloud/link/start", headers=http.session, json=body)


def poll(http):
    _skip_interval(http)
    return http.post("/api/cloud/link/poll", headers=http.session)


def test_start_returns_only_what_the_app_needs_and_calls_the_cloud(http, cloud):
    script_link(cloud)
    r = start(http, scope="read")
    assert r.status_code == 200
    assert r.json() == {"user_code": "ABCD-1234", "verification_uri": "https://cloud.test/link",
                        "expires_in": 600, "interval": 5}
    assert DEVICE_CODE not in r.text  # the secret device code stays on the server
    sent = cloud.seen("POST", "/auth/device/start")[0]
    assert json.loads(sent.content)["scope"] == "read" and json.loads(sent.content)["server_name"]
    assert "authorization" not in sent.headers  # nothing to authenticate with yet
    assert http.get("/api/cloud/link", headers=http.session).json()["state"] == "pending"


def test_poll_pending_then_linked_and_the_token_is_never_returned(http, cloud):
    script_link(cloud)
    cloud.on("POST", "/auth/device/token", status=400, json={"code": "authorization_pending", "message": "wait"})
    start(http)
    assert poll(http).json()["status"] == "pending"
    script_link(cloud)  # approved now
    done = poll(http)
    assert done.status_code == 200 and done.json()["status"] == "linked" and done.json()["handle"] == "david"
    st = http.get("/api/cloud/link", headers=http.session)
    assert st.json()["linked"] is True and st.json()["handle"] == "david" and st.json()["scope"] == "manage"
    assert st.json()["linked_at"]
    for text in (done.text, st.text):
        assert SERVER_TOKEN not in text and "gsrv_" not in text and DEVICE_CODE not in text and "cloud.test" not in text


def test_poll_too_soon_does_not_bother_the_cloud(http, cloud):
    script_link(cloud)
    start(http)
    r = http.post("/api/cloud/link/poll", headers=http.session)  # no skipping of the interval
    assert r.json()["status"] == "pending" and r.json()["retry_in"] >= 1
    assert cloud.seen("POST", "/auth/device/token") == []


def test_slow_down_raises_the_interval(http, cloud):
    script_link(cloud)
    cloud.on("POST", "/auth/device/token", status=400, json={"code": "slow_down", "message": "slow"})
    start(http)
    assert poll(http).json() == {"status": "pending", "interval": 10, "retry_in": None, "handle": None,
                                 "account_id": None, "scope": None, "linked_at": None, "state": None}


@pytest.mark.parametrize("code,expected", [("expired_token", "expired"), ("access_denied", "denied")])
def test_poll_expiry_and_denial_end_the_attempt(http, cloud, code, expected):
    script_link(cloud)
    cloud.on("POST", "/auth/device/token", status=400, json={"code": code, "message": "no"})
    start(http)
    assert poll(http).json()["status"] == expected
    assert http.get("/api/cloud/link", headers=http.session).json()["state"] == "none"
    assert poll(http).status_code == 409  # nothing in progress any more


def test_local_expiry_is_noticed_without_asking_the_cloud(http, cloud):
    script_link(cloud)
    start(http)
    con = sqlite3.connect(http.db)
    con.execute("UPDATE cloud_link SET pending_expires_at = '2000-01-01T00:00:00+00:00'")
    con.commit()
    con.close()
    assert poll(http).json()["status"] == "expired"
    assert cloud.seen("POST", "/auth/device/token") == []


def test_poll_with_cloud_down_is_a_clear_502(http, cloud):
    script_link(cloud)
    start(http)
    cloud.down = True
    r = poll(http)
    assert r.status_code == 502 and r.json()["code"] == "cloud_unavailable"
    assert "cloud.test" not in r.text and "boom" not in r.text


def test_token_is_encrypted_at_rest(http, cloud):
    link(http, cloud)
    con = sqlite3.connect(http.db)
    token_enc, device_enc = con.execute("SELECT token_enc, pending_device_enc FROM cloud_link").fetchone()
    con.close()
    assert token_enc and token_enc != SERVER_TOKEN and token_enc.startswith("gAAAA")  # a Fernet token
    assert device_enc is None  # the device code is gone once linked
    raw = raw_db_bytes(http)
    assert SERVER_TOKEN.encode() not in raw and DEVICE_CODE.encode() not in raw
    assert cloud_crypto.decrypt(token_enc) == SERVER_TOKEN


def test_device_code_is_encrypted_while_pending(http, cloud):
    script_link(cloud)
    start(http)
    assert DEVICE_CODE.encode() not in raw_db_bytes(http)


def test_key_file_is_created_0600_next_to_the_database(http, cloud):
    link(http, cloud)
    path = cloud_crypto.key_file()
    assert str(path.parent) == os.path.dirname(http.db)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_loose_key_file_is_tightened(http, cloud):
    link(http, cloud)
    path = cloud_crypto.key_file()
    os.chmod(path, 0o644)
    cloud_crypto.encrypt("x")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_key_from_the_environment_wins(http, cloud, monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setattr(settings, "cloud_key", Fernet.generate_key().decode())
    link(http, cloud)
    assert not cloud_crypto.key_file().exists()


def test_rotation_keeps_old_ciphertext_readable_and_reencrypts_under_the_new_key(tmp_path, monkeypatch):
    import asyncio
    from datetime import datetime, timezone

    from gaca_api.db import client
    from gaca_api.db import cloud as cloud_db
    from gaca_api.models.cloud import CloudLink
    from gaca_api.services import cloud_link as svc

    monkeypatch.setattr(settings, "db_path", str(tmp_path / "r.db"))
    monkeypatch.setattr(settings, "cloud_key", "")

    async def scenario():
        await client.connect()
        try:
            old = cloud_crypto.encrypt(SERVER_TOKEN)
            await cloud_db.save_link(CloudLink(
                user_id="u1", cloud_url=CLOUD_URL, status="linked", scope="manage", server_name="n",
                token_enc=old, created_at=datetime.now(timezone.utc)))
            keys = cloud_crypto.key_file().read_text().split()
            cloud_crypto.new_primary_key()
            assert len(cloud_crypto.key_file().read_text().split()) == len(keys) + 1
            assert cloud_crypto.decrypt(old) == SERVER_TOKEN  # the old key is still in the file
            assert await svc.reencrypt_all() == 1
            new = (await cloud_db.get_link("u1")).token_enc
            assert new != old and cloud_crypto.decrypt(new) == SERVER_TOKEN
            # dropping the old key now is safe: the new ciphertext opens with the new key alone
            cloud_crypto.key_file().write_text(cloud_crypto.key_file().read_text().split()[0] + "\n")
            assert cloud_crypto.decrypt(new) == SERVER_TOKEN
            with pytest.raises(cloud_crypto.CryptoError):
                cloud_crypto.decrypt(old)
        finally:
            await client.close()

    asyncio.run(scenario())
    assert stat.S_IMODE(cloud_crypto.key_file().stat().st_mode) == 0o600


def test_rotation_with_an_env_key_is_refused_with_instructions(monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setattr(settings, "cloud_key", Fernet.generate_key().decode())
    with pytest.raises(cloud_crypto.CryptoError, match="GACA_CLOUD_KEY"):
        cloud_crypto.new_primary_key()


def test_unreadable_key_gives_a_clear_error_not_a_crash(http, cloud):
    link(http, cloud)
    os.remove(cloud_crypto.key_file())  # a new key is generated: the old token cannot be read
    r = http.get("/api/artifacts/whatever/html", headers=http.session)
    assert r.status_code == 404  # not a pointer; local path
    cloud.on("GET", "/gacabits", json={"gacabits": []})
    # a pointer would need decrypting: force one via the DB
    con = sqlite3.connect(http.db)
    con.execute("INSERT INTO cloud_pointers VALUES ('g1','%s','g1','a','editor',NULL,'t',1,NULL,'2026-01-01T00:00:00+00:00','{}')"
                % con.execute("SELECT user_id FROM cloud_link").fetchone()[0])
    con.commit()
    con.close()
    r = http.get("/api/artifacts/g1/state", headers=http.session)
    assert r.status_code == 500 and r.json()["code"] == "cloud_key_unreadable"
    assert SERVER_TOKEN not in r.text


# ── auth on the link routes ──────────────────────────────────────────────────
def test_link_routes_need_a_paired_device(http, cloud):
    script_link(cloud)
    hermes = mint(http, scopes=("artifacts", "cloud"))
    assert http.post("/api/cloud/link/start", json={}).status_code == 401
    assert http.post("/api/cloud/link/start", headers=hermes, json={}).status_code == 403
    assert http.post("/api/cloud/link/poll", headers=hermes).status_code == 403
    assert http.delete("/api/cloud/link", headers=hermes).status_code == 403
    # any API token may ask whether the cloud is there; one without the cloud scope gets the trimmed answer
    assert http.get("/api/cloud/link", headers=hermes).status_code == 200
    plain = mint(http, name="plain", scopes=("artifacts",))
    assert http.get("/api/cloud/link", headers=plain).status_code == 200


def test_cloud_url_is_never_taken_from_the_request(http, cloud):
    script_link(cloud)
    r = start(http, scope="manage", cloud_url="https://evil.example/api/v1", url="https://evil.example")
    assert r.status_code == 200
    assert {req.url.host for req in cloud.requests} == {"cloud.test"}
    con = sqlite3.connect(http.db)
    assert con.execute("SELECT cloud_url FROM cloud_link").fetchone()[0] == CLOUD_URL
    con.close()


def test_start_when_already_linked_is_refused(http, cloud):
    link(http, cloud)
    r = start(http)
    assert r.status_code == 409 and r.json()["code"] == "already_linked"


# ── https pinning and the client's own rules ─────────────────────────────────
def test_http_cloud_url_is_refused_unless_the_dev_flag_is_on(monkeypatch):
    with pytest.raises(Exception) as e:
        cloud_client.CloudClient("http://cloud.test/api/v1")
    assert "cloud_misconfigured" in getattr(e.value, "code", "")
    monkeypatch.setattr(settings, "cloud_allow_insecure_dev", True)
    assert cloud_client.CloudClient("http://localhost:9/api/v1").base_url == "http://localhost:9/api/v1"


@pytest.mark.parametrize("url", ["https://user:pw@cloud.test/api", "https://cloud.test/api?x=1", "ftp://cloud.test", "cloud.test"])
def test_odd_urls_are_refused(url):
    with pytest.raises(ValueError):
        cloud_client.validate_base_url(url)


def test_start_with_an_insecure_configured_url_is_a_500_that_names_no_url(http, cloud, monkeypatch):
    monkeypatch.setattr(settings, "cloud_url", "http://cloud.test/api/v1")
    r = start(http)
    assert r.status_code == 500 and r.json()["code"] == "cloud_misconfigured"
    assert "cloud.test" not in r.text and cloud.requests == []


def test_redirects_are_not_followed(http, cloud):
    script_link(cloud)
    cloud.redirect_to = "https://evil.example/steal"
    r = start(http)
    assert r.status_code == 502
    assert {req.url.host for req in cloud.requests} == {"cloud.test"}


# ── unlinking ────────────────────────────────────────────────────────────────
def test_unlink_revokes_on_the_cloud_then_forgets(http, cloud):
    link(http, cloud, server_id="srv9")
    cloud.on("DELETE", "/me/servers/srv9", status=204)
    r = http.delete("/api/cloud/link", headers=http.session)
    assert r.status_code == 200 and r.json()["unlinked"] is True and r.json()["cloud_revoked"] is True
    sent = cloud.seen("DELETE", "/me/servers/srv9")[0]
    assert sent.headers["authorization"] == f"Bearer {SERVER_TOKEN}"  # the server token, to the cloud only
    assert http.get("/api/cloud/link", headers=http.session).json() == {
        "linked": False, "state": "none", "handle": None, "account_id": None, "linked_at": None, "scope": None,
        "agent_access": False, "cloud_access": True, "configured": True, "user_code": None,
        "verification_uri": None, "expires_at": None, "expires_in": None, "interval": None}


def test_unlink_with_the_cloud_down_still_deletes_locally_and_says_so(http, cloud):
    link(http, cloud)
    cloud.down = True
    r = http.delete("/api/cloud/link", headers=http.session)
    assert r.status_code == 200
    assert r.json()["unlinked"] is True and r.json()["cloud_revoked"] is False
    assert "website" in r.json()["message"] and "cloud.test" not in r.text
    assert http.get("/api/cloud/link", headers=http.session).json()["state"] == "none"
    con = sqlite3.connect(http.db)
    assert con.execute("SELECT count(*) FROM cloud_link").fetchone()[0] == 0
    con.close()


def test_unlink_when_the_cloud_refuses_self_revocation(http, cloud):
    link(http, cloud, server_id="srv9")
    cloud.on("DELETE", "/me/servers/srv9", status=403, json={"code": "forbidden_scope", "message": "session only"})
    r = http.delete("/api/cloud/link", headers=http.session)
    assert r.json()["unlinked"] is True and r.json()["cloud_revoked"] is False and "403" in r.json()["message"]


def test_unlink_removes_the_cloud_gacabits_from_the_list_but_not_local_ones(http, cloud):
    link(http, cloud)
    cloud.on("GET", "/gacabits", json={"gacabits": [gacabit()]})
    http.post("/api/artifacts", headers=http.session, json={"title": "Mine", "html": "<p>x</p>"})
    assert len(http.get("/api/artifacts", headers=http.session).json()["artifacts"]) == 2
    cloud.on("DELETE", "/me/servers/srv1", status=204)
    http.delete("/api/cloud/link?force=true", headers=http.session)  # a cloud page is present: force is required (8b)
    assert [a["title"] for a in http.get("/api/artifacts", headers=http.session).json()["artifacts"]] == ["Mine"]


def test_unlink_when_not_linked_is_a_plain_answer(http):
    r = http.delete("/api/cloud/link", headers=http.session)
    assert r.status_code == 200 and r.json()["unlinked"] is False


# ── the token never reaches a log line ───────────────────────────────────────
def test_token_and_cloud_url_never_appear_in_logs(http, cloud, caplog):
    caplog.set_level(logging.DEBUG)
    link(http, cloud)
    cloud.down = True
    http.get("/api/cloud/link", headers=http.session)
    http.delete("/api/cloud/link", headers=http.session)
    for record in caplog.records:
        text = record.getMessage() + str(record.args) + (record.exc_text or "")
        assert SERVER_TOKEN not in text and DEVICE_CODE not in text
    assert SERVER_TOKEN not in caplog.text and DEVICE_CODE not in caplog.text


def test_cloud_error_never_leaks_token_or_url(http, cloud):
    link(http, cloud)
    con = sqlite3.connect(http.db)
    con.execute("INSERT INTO cloud_pointers VALUES ('g1', (SELECT user_id FROM cloud_link), 'g1', 'a', 'editor', NULL, "
                "'t', 1, NULL, '2026-01-01T00:00:00+00:00', '{}')")
    con.commit()
    con.close()
    for mode in ("down", "slow"):
        cloud.down, cloud.slow = mode == "down", mode == "slow"
        r = http.post("/api/artifacts/g1/call", headers=http.session, json={"method": "data", "args": {}})
        assert r.status_code == 502 and r.json()["code"] == "cloud_unavailable"
        for secret in (SERVER_TOKEN, "cloud.test", "boom", "slow "):
            assert secret not in r.text
