"""History from a removed crew member: labelled, read-only, and never silently redirected to
another member. Against the real app and real auth."""
from tests.test_api_tokens_http import bearer, http, mint  # noqa: F401  (fixture + helpers)


def _crew(http):
    h = http.session
    assert http.post("/api/agent/connect", headers=h, json={"host": "http://127.0.0.1", "port": 8642, "api_key": "k" * 32, "name": "default"}).status_code == 201
    assert http.post("/api/crew", headers=h, json={"name": "david", "port": 8644, "api_key": "d" * 32}).status_code == 201


def _alert(http, **body):
    agent = bearer(mint(http, "poster", ["alerts"])["token"])
    r = http.post("/api/alerts", headers=agent, json={"kind": "x.y", "title": "t", **body})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def test_chats_with_a_removed_member_are_marked_and_become_read_only(http):
    _crew(http)
    h = http.session
    chat = http.post("/api/sessions", headers=h, json={"crew": "david"}).json()
    other = http.post("/api/sessions", headers=h, json={"crew": "default"}).json()
    assert chat["crew_removed"] is False
    assert http.delete("/api/crew/david", headers=h).status_code == 200

    rows = {s["id"]: s for s in http.get("/api/sessions", headers=h).json()["sessions"]}
    assert rows[chat["id"]]["crew_removed"] is True and rows[other["id"]]["crew_removed"] is False
    assert rows[chat["id"]]["crew"] == "david"  # kept, so the app can say who it was
    assert http.get(f"/api/sessions/{chat['id']}", headers=h).json()["crew_removed"] is True

    sent = http.post(f"/api/sessions/{chat['id']}/messages", headers=h, json={"content": "hello?"})
    assert sent.status_code == 409 and "read-only" in sent.json()["detail"]


def test_a_message_to_a_removed_members_chat_is_not_redirected_to_the_default_member(http):
    _crew(http)
    h = http.session
    chat = http.post("/api/sessions", headers=h, json={"crew": "david"}).json()
    http.delete("/api/crew/david", headers=h)
    http.post(f"/api/sessions/{chat['id']}/messages", headers=h, json={"content": "hello?"})
    detail = http.get(f"/api/sessions/{chat['id']}", headers=h).json()
    assert detail["messages"] == []  # nothing was stored, nothing was sent to anyone


def test_alerts_from_a_removed_member_are_marked_but_other_names_are_not(http):
    _crew(http)
    h = http.session
    mine, live, stranger = _alert(http, crew="david"), _alert(http, crew="default"), _alert(http, crew="never-existed")
    http.delete("/api/crew/david", headers=h)
    alerts = {a["id"]: a for a in http.get("/api/alerts", headers=h).json()["alerts"]}
    assert alerts[mine]["crew_removed"] is True
    assert alerts[live]["crew_removed"] is False and alerts[stranger]["crew_removed"] is False
    assert http.get(f"/api/alerts/{mine}", headers=h).json()["crew_removed"] is True


def test_removing_the_default_member_or_an_unknown_one_still_gets_the_old_errors(http):
    _crew(http)
    h = http.session
    assert http.delete("/api/crew/default", headers=h).status_code == 422  # still the default, others remain
    assert http.delete("/api/crew/ghost", headers=h).status_code == 404
