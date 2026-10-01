"""HTTP-level checks for photo attachments: upload, fetch, send with a message."""
import asyncio

import pytest
from fastapi.testclient import TestClient

from gaca_api.config import settings
from gaca_api.db import messages as messages_db
from gaca_api.main import app
from gaca_api.models.user import CrewMember, UserDoc
from gaca_api.schemas.event import RunCompleted
from gaca_api.services import auth as auth_service
from gaca_api.utils.auth import get_current_user

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
_current = {"id": "u1"}


class StubBackend:
    def __init__(self):
        self.images = []

    async def start_run(self, *, session_id, history, user_message, images=()):
        self.images = list(images)
        return "run_stub"

    async def stream_events(self, run_id, *, session_id):
        yield RunCompleted(session_id=session_id, run_id=run_id, output="A red square.")


@pytest.fixture
def http(tmp_path, monkeypatch):
    settings.db_path = str(tmp_path / "t.db")
    backend = StubBackend()
    monkeypatch.setattr(auth_service, "backend_for_crew", lambda user, member: backend)

    def current():
        return UserDoc(
            id=_current["id"], email=f"{_current['id']}@example.com", display_name="T",
            agent_host="h", crew=[CrewMember(name="default", port=1, api_key="k")],
            default_crew="default",
        )

    app.dependency_overrides[get_current_user] = current
    _current["id"] = "u1"
    with TestClient(app) as c:
        c.backend = backend
        yield c
    app.dependency_overrides.clear()


def _session(http) -> str:
    return http.post("/api/sessions", json={}).json()["id"]


def test_upload_fetch_and_send_a_photo_with_a_message(http):
    sid = _session(http)
    up = http.post(f"/api/sessions/{sid}/attachments", content=JPEG, headers={"Content-Type": "image/jpeg"})
    assert up.status_code == 201
    att = up.json()
    assert att["content_type"] == "image/jpeg" and att["size"] == len(JPEG)
    assert att["url"] == f"sessions/{sid}/attachments/{att['id']}"

    got = http.get(f"/api/{att['url']}")
    assert got.status_code == 200 and got.content == JPEG
    assert got.headers["content-type"] == "image/jpeg"

    sent = http.post(f"/api/sessions/{sid}/messages", json={"content": "what is this?", "attachment_ids": [att["id"]]})
    assert sent.status_code == 202
    assert [i.data for i in http.backend.images] == [JPEG]

    detail = http.get(f"/api/sessions/{sid}").json()
    user_msg = detail["messages"][0]
    assert user_msg["attachments"][0]["id"] == att["id"]
    assert user_msg["attachments"][0]["url"] == att["url"]
    # and the Chat list labels a photo-only chat sensibly
    again = http.post(f"/api/sessions/{sid}/messages", json={"content": "", "attachment_ids": []})
    assert again.status_code == 422  # nothing to send


def test_bad_uploads_and_foreign_access_are_refused(http):
    sid = _session(http)
    assert http.post(f"/api/sessions/{sid}/attachments", content=b"<html>", headers={"Content-Type": "image/jpeg"}).status_code == 422
    big = JPEG + b"0" * (2 * 1024 * 1024)
    assert http.post(f"/api/sessions/{sid}/attachments", content=big, headers={"Content-Type": "image/jpeg"}).status_code == 413

    att = http.post(f"/api/sessions/{sid}/attachments", content=JPEG, headers={"Content-Type": "image/jpeg"}).json()
    _current["id"] = "u2"
    assert http.get(f"/api/{att['url']}").status_code == 404
    assert http.post(f"/api/sessions/{sid}/attachments", content=JPEG, headers={"Content-Type": "image/jpeg"}).status_code == 404
    _current["id"] = "u1"
    # a message can't reference an unknown photo, or the same one twice
    assert http.post(f"/api/sessions/{sid}/messages", json={"content": "x", "attachment_ids": ["nope"]}).status_code == 422
    assert http.post(f"/api/sessions/{sid}/messages", json={"content": "x", "attachment_ids": [att["id"]]}).status_code == 202
    assert http.post(f"/api/sessions/{sid}/messages", json={"content": "y", "attachment_ids": [att["id"]]}).status_code == 422
