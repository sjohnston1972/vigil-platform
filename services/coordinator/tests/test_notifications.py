import pytest
from unittest.mock import AsyncMock, patch, MagicMock


STEP_UP_REQ = {
    "id": "sur-001",
    "tenant_id": "tenant-a",
    "tool_name": "apply_change",
    "requested_by": "user@client.com",
    "context": {"change_id": "chg-001", "summary": "Shut down Gi0/1"},
    "expires_at": "2026-03-17T10:15:00+00:00",
}

TENANT_CONFIG_EMAIL = {
    "step_up_notification_email": "approvals@client.com",
    "step_up_webhook_url": None,
}

TENANT_CONFIG_WEBHOOK = {
    "step_up_notification_email": None,
    "step_up_webhook_url": "https://hooks.example.com/vigil",
}


class TestNotifyApprovers:
    @pytest.mark.asyncio
    async def test_sends_email_when_configured(self):
        from notifications import notify_approvers

        with patch("notifications._send_email", new_callable=AsyncMock) as mock_email:
            await notify_approvers(STEP_UP_REQ, TENANT_CONFIG_EMAIL)

        mock_email.assert_called_once()
        args = mock_email.call_args
        assert "approvals@client.com" in args[0]
        body = args[0][1]
        assert body["tool"] == "apply_change"
        assert "approve_url" in body
        assert "reject_url" in body

    @pytest.mark.asyncio
    async def test_sends_webhook_when_configured(self):
        from notifications import notify_approvers

        with patch("notifications._post_webhook", new_callable=AsyncMock) as mock_hook:
            await notify_approvers(STEP_UP_REQ, TENANT_CONFIG_WEBHOOK)

        mock_hook.assert_called_once()
        url_arg = mock_hook.call_args[0][0]
        assert url_arg == "https://hooks.example.com/vigil"

    @pytest.mark.asyncio
    async def test_does_not_raise_on_email_failure(self):
        from notifications import notify_approvers

        with patch("notifications._send_email", new_callable=AsyncMock, side_effect=Exception("SMTP down")):
            # Must not propagate — fire-and-forget
            await notify_approvers(STEP_UP_REQ, TENANT_CONFIG_EMAIL)
