"""The published schema is how an agent discovers this API.

The agent setup guide deliberately pins no endpoint paths — it tells the agent
to read /api/openapi.json instead. That makes the generated schema part of the
contract: if router mounting regresses, agents break, and no other test would
notice because the service layer would still be fine.

Importing the app is safe here — the FastAPI object is built at import time, but
lifespan (DB connect, agent backend) only runs when the server serves.
"""
from gaca_api.main import app


def test_every_route_is_mounted_under_api():
    for path in app.openapi()["paths"]:
        assert path.startswith("/api/"), f"{path} is unreachable through nginx"


def test_schema_lists_the_auth_and_session_paths():
    paths = app.openapi()["paths"]
    for expected in (
        "/api/auth/register",
        "/api/auth/login",
        "/api/auth/me",
        "/api/auth/google",
        "/api/auth/agent-token",
        "/api/agent",
        "/api/agent/connect",
        "/api/crew",
        "/api/crew/discover",
        "/api/crew/{name}",
        "/api/crew/{name}/default",
        "/api/crew/{name}/skills",
        "/api/crew/{name}/profile",
        "/api/alerts",
        "/api/alerts/{alert_id}",
        "/api/alerts/{alert_id}/act",
        "/api/alerts/{alert_id}/dismiss",
        "/api/alerts/{alert_id}/snooze",
        "/api/alerts/{alert_id}/delegate",
        "/api/alerts/{alert_id}/take-to-chat",
        "/api/lists",
        "/api/lists/match",
        "/api/lists/{list_id}",
        "/api/lists/{list_id}/items",
        "/api/lists/{list_id}/items/{item_id}",
        "/api/lists/{list_id}/items/check",
        "/api/lists/{list_id}/items/remove",
        "/api/lists/{list_id}/clear-done",
        "/api/briefings",
        "/api/briefings/dates",
        "/api/briefings/{date}",
        "/api/briefings/{date}/sections/{key}",
        "/api/sessions",
        "/api/health",
    ):
        assert expected in paths


def test_discovery_urls_are_where_the_agent_guide_says():
    assert app.openapi_url == "/api/openapi.json"
    assert app.docs_url == "/api/docs"
    assert app.redoc_url is None


def test_get_agent_is_the_resource_not_the_docs_page():
    """Swagger UI used to be mounted at /api/agent, which shadowed GET /api/agent
    (the agent status every client reads) with an HTML page."""
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        response = client.get("/api/agent")
    assert "text/html" not in response.headers.get("content-type", "")
