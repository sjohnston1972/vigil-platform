import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, patch


def make_app():
    from main import app
    return app


class TestChatStreamEndpoint:
    def test_chat_stream_returns_event_stream(self):
        async def fake_generator(request, tenant_config):
            yield 'data: {"type": "session_start", "session_id": "s1", "tenant_id": "t1"}\n\n'
            yield 'data: {"type": "token", "content": "hi"}\n\n'
            yield 'data: {"type": "done", "tokens_used": 3, "session_id": "s1"}\n\n'

        with patch("main._get_tenant_config", new_callable=AsyncMock, return_value={}):
            with patch("main._stream_generator", new=fake_generator):
                client = TestClient(make_app())
                response = client.post(
                    "/chat/stream",
                    json={
                        "session_id": "s1",
                        "tenant_id": "t1",
                        "messages": [{"role": "user", "content": "hi"}],
                    },
                )

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert '"type": "session_start"' in response.text
        assert '"type": "done"' in response.text

    def test_chat_stream_emits_error_event_when_tenant_config_load_fails(self):
        with patch("main._get_tenant_config", new_callable=AsyncMock, side_effect=Exception("cosmos down")):
            client = TestClient(make_app())
            response = client.post(
                "/chat/stream",
                json={"session_id": "s1", "tenant_id": "t1", "messages": [{"role": "user", "content": "hi"}]},
            )

        assert response.status_code == 200
        assert '"type": "error"' in response.text
        assert '"code": "coordinator_unavailable"' in response.text


class TestStepUpApproveEndpoint:
    def test_approve_returns_200_for_valid_approver(self):
        pending_record = {
            "id": "sur-001", "tenant_id": "tenant-a",
            "status": "pending", "expires_at": "2099-01-01T00:00:00+00:00",
            "requested_by": "user@client.com",
            "tool_name": "apply_change",
            "context": {"change_id": "chg-001"},
            "approved_by": None, "approved_at": None,
        }
        tenant_config = {
            "tenant_id": "tenant-a",
            "step_up_approvers": ["approver@client.com"],
            "step_up_policy": {
                "apply_change": {"self_approve": False}
            },
        }

        with patch("main._get_step_up_request", new_callable=AsyncMock, return_value=pending_record):
            with patch("main._get_tenant_config", new_callable=AsyncMock, return_value=tenant_config):
                with patch("main._write_step_up_decision", new_callable=AsyncMock):
                    with patch("main._propagate_approval_to_change_records", new_callable=AsyncMock):
                        client = TestClient(make_app())
                        response = client.post(
                            "/step-up/sur-001/approve",
                            json={"comment": None},
                            headers={"X-Tenant-Id": "tenant-a", "X-User-Identity": "approver@client.com"},
                        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "approved"
        assert data["decided_by"] == "approver@client.com"

    def test_approve_returns_403_for_self_approval_on_high_risk_tool(self):
        pending_record = {
            "id": "sur-002", "tenant_id": "tenant-a",
            "status": "pending", "expires_at": "2099-01-01T00:00:00+00:00",
            "requested_by": "user@client.com",
            "tool_name": "apply_change",
            "context": {}, "approved_by": None, "approved_at": None,
        }
        tenant_config = {
            "tenant_id": "tenant-a",
            "step_up_approvers": ["approver@client.com", "user@client.com"],
            "step_up_policy": {"apply_change": {"self_approve": False}},
        }

        with patch("main._get_step_up_request", new_callable=AsyncMock, return_value=pending_record):
            with patch("main._get_tenant_config", new_callable=AsyncMock, return_value=tenant_config):
                client = TestClient(make_app())
                response = client.post(
                    "/step-up/sur-002/approve",
                    json={},
                    headers={"X-Tenant-Id": "tenant-a", "X-User-Identity": "user@client.com"},
                )

        assert response.status_code == 403
        data = response.json()
        assert data["detail"]["error"] == "self_approval_not_permitted"

    def test_approve_returns_403_and_logs_audit(self):
        """Forbidden attempts must be written to the audit log (structured logger.warning)."""
        pending_record = {
            "id": "sur-004", "tenant_id": "tenant-a",
            "status": "pending", "expires_at": "2099-01-01T00:00:00+00:00",
            "requested_by": "user@client.com",
            "tool_name": "apply_change",
            "context": {}, "approved_by": None, "approved_at": None,
        }
        tenant_config = {
            "tenant_id": "tenant-a",
            "step_up_approvers": ["approver@client.com"],
            "step_up_policy": {"apply_change": {"self_approve": False}},
        }

        with patch("main._get_step_up_request", new_callable=AsyncMock, return_value=pending_record):
            with patch("main._get_tenant_config", new_callable=AsyncMock, return_value=tenant_config):
                with patch("main.logger") as mock_logger:
                    client = TestClient(make_app())
                    response = client.post(
                        "/step-up/sur-004/approve",
                        json={},
                        headers={"X-Tenant-Id": "tenant-a", "X-User-Identity": "unknown@client.com"},
                    )

        assert response.status_code == 403
        mock_logger.warning.assert_called_once()
        call_kwargs = mock_logger.warning.call_args
        extra = call_kwargs[1].get("extra", {})
        assert extra.get("reason") == "not_authorised_approver"

    def test_approve_returns_409_on_duplicate(self):
        already_approved = {
            "id": "sur-003", "tenant_id": "tenant-a",
            "status": "approved", "expires_at": "2099-01-01T00:00:00+00:00",
            "requested_by": "user@client.com",
            "tool_name": "apply_change",
            "context": {}, "approved_by": "approver@client.com", "approved_at": "...",
        }
        tenant_config = {
            "tenant_id": "tenant-a",
            "step_up_approvers": ["approver@client.com"],
            "step_up_policy": {"apply_change": {"self_approve": False}},
        }

        with patch("main._get_step_up_request", new_callable=AsyncMock, return_value=already_approved):
            with patch("main._get_tenant_config", new_callable=AsyncMock, return_value=tenant_config):
                client = TestClient(make_app())
                response = client.post(
                    "/step-up/sur-003/approve",
                    json={},
                    headers={"X-Tenant-Id": "tenant-a", "X-User-Identity": "approver@client.com"},
                )

        assert response.status_code == 409
        assert response.json()["detail"]["error"] == "already_decided"
