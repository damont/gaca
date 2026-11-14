"""Part 8c: Hermes may ask whether the cloud is connected; it only gets to USE the cloud after the owner
switches "let my agent use the cloud" on in the app. Off by default."""
import sqlite3

from tests.cloud_support import (  # noqa: F401  (fixtures)
    SERVER_TOKEN, cloud, gacabit, http, link, mint,
)


def status(http, headers):
    r = http.get("/api/cloud/link", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def scopes_of(http, name):
    con = sqlite3.connect(http.db)
    try:
        import json
        return [json.loads(r[0]) for r in con.execute("SELECT scopes FROM api_tokens WHERE name = ? AND revoked_at IS NULL", (name,))]
    finally:
        con.close()


def test_unlinked_agent_can_ask_and_is_told_nothing_more(http, cloud):
    hermes = mint(http, name="hermes-x", scopes=("artifacts",))
    assert status(http, hermes) == {"linked": False, "state": "none", "agent_access": False, "cloud_access": False,
                                    "handle": None, "account_id": None, "linked_at": None, "scope": None,
                                    "configured": True, "user_code": None, "verification_uri": None,
                                    "expires_at": None, "expires_in": None, "interval": None}


def test_linked_but_off_by_default_the_agent_sees_no_account_details(http, cloud):
    link(http, cloud)
    hermes = mint(http, name="hermes-x", scopes=("artifacts",))
    st = status(http, hermes)
    assert st["linked"] is True and st["agent_access"] is False and st["cloud_access"] is False
    assert st["handle"] is None and st["account_id"] is None and st["scope"] is None
    assert SERVER_TOKEN not in str(st)
    app = status(http, http.session)
    assert app["handle"] == "david" and app["agent_access"] is False and app["cloud_access"] is True


def test_turning_it_on_adds_cloud_to_hermes_tokens_in_place(http, cloud):
    link(http, cloud)
    hermes = mint(http, name="hermes-x", scopes=("artifacts",))
    other = mint(http, name="my-script", scopes=("artifacts",))  # not a Hermes token: never touched
    r = http.put("/api/cloud/agent-access", headers=http.session, json={"enabled": True})
    assert r.status_code == 200 and r.json()["agent_access"] is True
    st = status(http, hermes)  # the very same secret now has the scope
    assert st["cloud_access"] is True and st["handle"] == "david"
    assert "cloud" not in scopes_of(http, "my-script")[0]
    assert status(http, other)["cloud_access"] is False
    # and back off
    http.put("/api/cloud/agent-access", headers=http.session, json={"enabled": False})
    assert status(http, hermes)["cloud_access"] is False and status(http, hermes)["agent_access"] is False


def test_only_the_app_can_flip_it_and_only_when_linked(http, cloud):
    hermes = mint(http, name="hermes-x", scopes=("artifacts", "cloud"))
    assert http.put("/api/cloud/agent-access", headers=hermes, json={"enabled": True}).status_code == 403
    assert http.put("/api/cloud/agent-access", json={"enabled": True}).status_code == 401
    r = http.put("/api/cloud/agent-access", headers=http.session, json={"enabled": True})
    assert r.status_code == 409 and r.json()["code"] == "not_linked"
    assert http.put("/api/cloud/agent-access", headers=http.session, json={"enabled": False}).status_code == 200


def test_unlink_takes_the_scope_away_and_a_relink_starts_off(http, cloud):
    link(http, cloud)
    hermes = mint(http, name="hermes-x", scopes=("artifacts",))
    http.put("/api/cloud/agent-access", headers=http.session, json={"enabled": True})
    assert http.delete("/api/cloud/link", headers=http.session).status_code == 200
    assert status(http, hermes)["cloud_access"] is False
    link(http, cloud)
    assert status(http, hermes)["agent_access"] is False and status(http, hermes)["cloud_access"] is False


def test_a_reminted_hermes_token_follows_the_setting(tmp_path, monkeypatch):
    """`hermes connect` (and the installer) mint with `_default_scopes`: cloud only if the owner switched it on."""
    import asyncio
    from datetime import timedelta

    from conftest import make_owner
    from gaca_api.config import settings
    from gaca_api.db import client, cloud as cloud_db
    from gaca_api.models.base import utcnow
    from gaca_api.models.cloud import CloudLink
    from gaca_api.provisioning import hermes_connect

    monkeypatch.setattr(settings, "db_path", str(tmp_path / "gaca.db"))

    async def go():
        await client.connect()
        try:
            owner = await make_owner(name="O", email="o@example.com")
            assert "cloud" not in await hermes_connect._default_scopes(owner)  # not linked
            link = CloudLink(user_id=owner.id, cloud_url="https://cloud.test/api/v1", status="linked", scope="manage",
                             server_name="n", created_at=utcnow(), agent_access=False)
            await cloud_db.save_link(link)
            assert "cloud" not in await hermes_connect._default_scopes(owner)  # linked, switch off
            await cloud_db.save_link(link.model_copy(update={"agent_access": True}))
            assert "cloud" in await hermes_connect._default_scopes(owner)
        finally:
            await client.close()

    asyncio.run(go())


# ── Part 8d: configured, the pending attempt, published_template_id ──────────
def test_status_says_whether_the_cloud_is_configured(http, cloud, monkeypatch):
    from gaca_api.config import settings
    assert http.get("/api/cloud/link", headers=http.session).json()["configured"] is True
    monkeypatch.setattr(settings, "cloud_url", "")
    assert http.get("/api/cloud/link", headers=http.session).json()["configured"] is False
    hermes = mint(http, name="hermes-x", scopes=("artifacts",))
    assert http.get("/api/cloud/link", headers=hermes).json()["configured"] is False


def test_a_pending_link_can_be_shown_again_to_the_app_only(http, cloud):
    from tests.cloud_support import DEVICE_CODE, script_link
    script_link(cloud)
    http.post("/api/cloud/link/start", headers=http.session, json={})
    app = http.get("/api/cloud/link", headers=http.session).json()
    assert app["state"] == "pending" and app["user_code"] == "ABCD-1234"
    assert app["verification_uri"] == "https://cloud.test/link" and app["expires_at"] and app["interval"] == 5
    assert 0 < app["expires_in"] <= 600
    for headers in (mint(http, name="hermes-x", scopes=("artifacts", "cloud")), mint(http, name="p", scopes=("artifacts",))):
        r = http.get("/api/cloud/link", headers=headers)
        assert r.status_code == 200 and "user_code" not in r.text.replace('"user_code":null', "") and "ABCD" not in r.text
    assert DEVICE_CODE not in http.get("/api/cloud/link", headers=http.session).text


def test_an_expired_or_finished_attempt_shows_no_code(http, cloud):
    import sqlite3
    from tests.cloud_support import script_link
    script_link(cloud)
    http.post("/api/cloud/link/start", headers=http.session, json={})
    con = sqlite3.connect(http.db)
    con.execute("UPDATE cloud_link SET pending_expires_at = '2000-01-01T00:00:00+00:00'")
    con.commit(); con.close()
    assert http.get("/api/cloud/link", headers=http.session).json()["user_code"] is None
    link(http, cloud)  # a fresh, completed link
    done = http.get("/api/cloud/link", headers=http.session).json()
    assert done["state"] == "linked" and done["user_code"] is None


def test_the_list_carries_published_template_id(http, cloud):
    link(http, cloud)
    g = gacabit("a" * 32, title="Shared", published_template_id="t" * 32)
    cloud.on("GET", "/gacabits", json={"gacabits": [g], "next_cursor": None})
    items = http.get("/api/artifacts", headers=http.session).json()["artifacts"]
    row = next(i for i in items if i["id"] == "a" * 32)
    assert row["published_template_id"] == "t" * 32 and row["home"] == "cloud"
