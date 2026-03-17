import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, patch, MagicMock


class TestStepUpProxy:
    def test_approve_proxies_to_coordinator(self):
        from main import app

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "request_id": "sur-001",
            "status": "approved",
            "decided_by": "approver@client.com",
            "decided_at": "2026-03-17T10:01:00+00:00",
        }

        with patch("main._proxy_json", new_callable=AsyncMock, return_value=mock_response):
            with patch("main.validate_ise_token", return_value={
                "tenant_id": "tenant-a",
                "user_identity": "approver@client.com",
            }):
                client = TestClient(app)
                response = client.post(
                    "/step-up/sur-001/approve",
                    json={"comment": None},
                    headers={"Authorization": "Bearer valid-token"},
                )

        # Gateway should forward whatever the coordinator returns
        assert response.status_code == 200
        assert response.json()["status"] == "approved"

    def test_unauthenticated_request_returns_401(self):
        from main import app

        client = TestClient(app)
        response = client.post("/step-up/sur-001/approve", json={})

        assert response.status_code == 401

    def test_reject_proxies_to_coordinator(self):
        from main import app

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "request_id": "sur-001",
            "status": "rejected",
            "decided_by": "approver@client.com",
            "decided_at": "2026-03-17T10:01:00+00:00",
        }

        with patch("main._proxy_json", new_callable=AsyncMock, return_value=mock_response):
            with patch("main.validate_ise_token", return_value={
                "tenant_id": "tenant-a",
                "user_identity": "approver@client.com",
            }):
                client = TestClient(app)
                response = client.post(
                    "/step-up/sur-001/reject",
                    json={"comment": "Not approved"},
                    headers={"Authorization": "Bearer valid-token"},
                )

        assert response.status_code == 200
        assert response.json()["status"] == "rejected"
