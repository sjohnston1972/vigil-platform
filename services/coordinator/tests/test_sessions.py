from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, MagicMock, patch
from azure.cosmos.exceptions import CosmosResourceNotFoundError


def make_client():
    from main import app
    return TestClient(app)


def _async_iter(items):
    """Return an object that satisfies `async for item in ...`."""
    async def _gen():
        for item in items:
            yield item
    return _gen()


def test_get_sessions_returns_list():
    mock_items = [
        {"id": "sess_1", "tenant_id": "t1", "title": "BGP audit", "agents": ["network_agent"], "updated_at": "2026-03-18T00:00:00Z"},
    ]
    with patch("main.conversations_container") as mock_container:
        mock_container.query_items.return_value = _async_iter(mock_items)
        resp = make_client().get("/sessions", headers={"X-Tenant-Id": "t1"})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["title"] == "BGP audit"


def test_patch_session_title():
    mock_item = {"id": "sess_1", "tenant_id": "t1", "title": "Old title", "agents": [], "updated_at": "2026-03-18T00:00:00Z"}
    with patch("main.conversations_container") as mock_container:
        mock_container.read_item = AsyncMock(return_value=dict(mock_item))
        mock_container.replace_item = AsyncMock(return_value=None)
        resp = make_client().patch("/sessions/sess_1/title", json={"title": "New title"}, headers={"X-Tenant-Id": "t1"})
    assert resp.status_code == 200
    assert resp.json()["title"] == "New title"


def test_patch_session_title_rejects_wrong_tenant():
    with patch("main.conversations_container") as mock_container:
        mock_container.read_item = AsyncMock(side_effect=CosmosResourceNotFoundError(404, "not found"))
        resp = make_client().patch("/sessions/sess_1/title", json={"title": "Hijack"}, headers={"X-Tenant-Id": "t1"})
    assert resp.status_code == 404


def test_get_sessions_requires_tenant_header():
    resp = make_client().get("/sessions")
    assert resp.status_code == 422


def test_auth_me_returns_tenant_id():
    resp = make_client().get("/auth/me", headers={"X-Tenant-Id": "acme-corp"})
    assert resp.status_code == 200
    assert resp.json()["tenant_id"] == "acme-corp"


def test_auth_me_requires_tenant_header():
    resp = make_client().get("/auth/me")
    assert resp.status_code == 422
