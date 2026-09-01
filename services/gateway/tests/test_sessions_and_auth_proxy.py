from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient


VALID_CLAIMS = {"tenant_id": "tenant-a", "user_identity": "alice@tenant-a.com"}


class TestAuthMeProxy:
    def test_returns_tenant_id_from_coordinator(self):
        from main import app

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"tenant_id": "tenant-a"}

        with patch("main._proxy_get", new_callable=AsyncMock, return_value=mock_response):
            with patch("main.validate_ise_token", return_value=VALID_CLAIMS):
                client = TestClient(app)
                response = client.get("/auth/me", headers={"Authorization": "Bearer whatever"})

        assert response.status_code == 200
        assert response.json() == {"tenant_id": "tenant-a"}

    def test_missing_token_returns_401(self):
        from main import app

        client = TestClient(app)
        response = client.get("/auth/me")
        assert response.status_code == 401


class TestSessionsListProxy:
    def test_returns_coordinator_sessions(self):
        from main import app

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {"id": "s1", "tenant_id": "tenant-a", "title": "First chat", "agents": [], "updated_at": "2026-01-01T00:00:00Z"},
        ]

        with patch("main._proxy_get", new_callable=AsyncMock, return_value=mock_response) as proxy_get:
            with patch("main.validate_ise_token", return_value=VALID_CLAIMS):
                client = TestClient(app)
                response = client.get("/sessions", headers={"Authorization": "Bearer whatever"})

        assert response.status_code == 200
        assert response.json()[0]["id"] == "s1"
        proxy_get.assert_awaited_once()
        assert proxy_get.await_args.args[0] == "/sessions"

    def test_missing_token_returns_401(self):
        from main import app

        client = TestClient(app)
        response = client.get("/sessions")
        assert response.status_code == 401


class TestSessionRenameProxy:
    def test_renames_and_forwards_to_coordinator(self):
        from main import app

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "id": "s1", "tenant_id": "tenant-a", "title": "New title", "agents": [], "updated_at": "2026-01-01T00:01:00Z",
        }

        with patch("main._proxy_json", new_callable=AsyncMock, return_value=mock_response) as proxy_json:
            with patch("main.validate_ise_token", return_value=VALID_CLAIMS):
                client = TestClient(app)
                response = client.patch(
                    "/sessions/s1/title",
                    json={"title": "New title"},
                    headers={"Authorization": "Bearer whatever"},
                )

        assert response.status_code == 200
        assert response.json()["title"] == "New title"
        proxy_json.assert_awaited_once()
        assert proxy_json.await_args.args[0] == "patch"
        assert proxy_json.await_args.args[1] == "/sessions/s1/title"

    def test_missing_token_returns_401(self):
        from main import app

        client = TestClient(app)
        response = client.patch("/sessions/s1/title", json={"title": "x"})
        assert response.status_code == 401

    def test_coordinator_404_is_relayed(self):
        from main import app

        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.json.return_value = {"detail": "session not found"}

        with patch("main._proxy_json", new_callable=AsyncMock, return_value=mock_response):
            with patch("main.validate_ise_token", return_value=VALID_CLAIMS):
                client = TestClient(app)
                response = client.patch(
                    "/sessions/does-not-exist/title",
                    json={"title": "x"},
                    headers={"Authorization": "Bearer whatever"},
                )

        assert response.status_code == 404
