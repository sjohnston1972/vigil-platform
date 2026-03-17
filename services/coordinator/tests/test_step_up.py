import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone
from dataclasses import dataclass


# Minimal ChatRequest stub for tests
@dataclass
class ChatRequest:
    tenant_id: str
    session_id: str
    user_identity: str = "jsmith@client.com"


class TestCreateStepUpRequest:
    @pytest.mark.asyncio
    async def test_creates_record_with_correct_fields(self):
        from step_up import create_step_up_request

        mock_container = AsyncMock()
        mock_container.create_item = AsyncMock(side_effect=lambda body, **_: body)

        policy = {
            "grant_type": "single_use",
            "pending_ttl_seconds": 900,
            "grant_duration_seconds": None,
        }
        request = ChatRequest(tenant_id="tenant-a", session_id="s-001")
        tool_input = {"change_id": "chg-001", "device_host": "10.0.0.1"}

        with patch("step_up._step_up_container", mock_container):
            record = await create_step_up_request("apply_change", tool_input, request, policy)

        assert record["tenant_id"] == "tenant-a"
        assert record["session_id"] == "s-001"
        assert record["tool_name"] == "apply_change"
        assert record["requested_by"] == "jsmith@client.com"
        assert record["status"] == "pending"
        assert record["grant_type"] == "single_use"
        assert record["id"].startswith("sur-")
        assert "expires_at" in record
        mock_container.create_item.assert_called_once()

    @pytest.mark.asyncio
    async def test_context_contains_tool_input_summary(self):
        from step_up import create_step_up_request

        mock_container = AsyncMock()
        mock_container.create_item = AsyncMock(side_effect=lambda body, **_: body)

        policy = {"grant_type": "single_use", "pending_ttl_seconds": 300, "grant_duration_seconds": None}
        request = ChatRequest(tenant_id="tenant-a", session_id="s-002")

        with patch("step_up._step_up_container", mock_container):
            record = await create_step_up_request("rollback_change", {"change_id": "chg-002"}, request, policy)

        assert "change_id" in record["context"] or "summary" in record["context"]


class TestGetActiveGrant:
    @pytest.mark.asyncio
    async def test_returns_grant_when_active(self):
        from step_up import get_active_grant

        mock_grant = {
            "id": "grnt-001",
            "tenant_id": "tenant-a",
            "session_id": "s-001",
            "tool_name": "bulk_close_tickets",
            "approved_by": "approver@client.com",
            "expires_at": "2099-01-01T00:00:00+00:00",
        }
        mock_container = MagicMock()
        mock_container.query_items = MagicMock(return_value=[mock_grant])

        with patch("step_up._grants_container", mock_container):
            result = await get_active_grant("tenant-a", "s-001", "bulk_close_tickets")

        assert result == mock_grant
        mock_container.query_items.assert_called_once()
        call_kwargs = mock_container.query_items.call_args.kwargs
        assert call_kwargs["partition_key"] == "tenant-a"

    @pytest.mark.asyncio
    async def test_returns_none_when_no_grant(self):
        from step_up import get_active_grant

        mock_container = MagicMock()
        mock_container.query_items = MagicMock(return_value=[])

        with patch("step_up._grants_container", mock_container):
            result = await get_active_grant("tenant-a", "s-001", "apply_change")

        assert result is None


class TestMarkStepUpFailed:
    @pytest.mark.asyncio
    async def test_sets_status_to_failed(self):
        from step_up import mark_step_up_failed

        existing = {"id": "sur-001", "tenant_id": "tenant-a", "status": "approved"}
        mock_container = AsyncMock()
        mock_container.read_item = AsyncMock(return_value=dict(existing))
        mock_container.replace_item = AsyncMock()

        with patch("step_up._step_up_container", mock_container):
            await mark_step_up_failed("sur-001", "tenant-a")

        replaced = mock_container.replace_item.call_args.kwargs["body"]
        assert replaced["status"] == "failed"
        mock_container.read_item.assert_called_once_with(item="sur-001", partition_key="tenant-a")


class TestAwaitStepUpDecision:
    @pytest.mark.asyncio
    async def test_returns_approved_when_status_changes(self):
        from step_up import await_step_up_decision

        pending = {"id": "sur-001", "tenant_id": "tenant-a", "status": "pending",
                   "expires_at": "2099-01-01T00:00:00+00:00", "approved_by": None}
        approved = {**pending, "status": "approved", "approved_by": "approver@client.com",
                    "approved_at": "2026-03-17T10:01:00+00:00"}

        call_count = 0
        async def mock_read_item(item, partition_key):
            nonlocal call_count
            call_count += 1
            return pending if call_count < 2 else approved

        mock_container = AsyncMock()
        mock_container.read_item = mock_read_item

        policy = {"pending_ttl_seconds": 900}

        with patch("step_up._step_up_container", mock_container):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                result = await await_step_up_decision("sur-001", "tenant-a", policy)

        assert result["status"] == "approved"
        assert result["approved_by"] == "approver@client.com"

    @pytest.mark.asyncio
    async def test_returns_expired_when_past_expires_at(self):
        from step_up import await_step_up_decision

        expired_record = {
            "id": "sur-002", "tenant_id": "tenant-a", "status": "pending",
            "expires_at": "2000-01-01T00:00:00+00:00",
            "approved_by": None,
        }
        mock_container = AsyncMock()
        mock_container.read_item = AsyncMock(return_value=expired_record)
        mock_container.replace_item = AsyncMock()

        policy = {"pending_ttl_seconds": 900}

        with patch("step_up._step_up_container", mock_container):
            result = await await_step_up_decision("sur-002", "tenant-a", policy)

        assert result["status"] == "expired"


class TestFetchToolCredential:
    @pytest.mark.asyncio
    async def test_fetches_secret_with_hyphenated_name(self):
        from step_up import fetch_tool_credential

        mock_kv = AsyncMock()
        mock_kv.get_secret = AsyncMock(return_value=MagicMock(value="secret-value"))

        with patch("step_up._kv_client", mock_kv):
            result = await fetch_tool_credential("apply_change", "acme")

        assert result == "secret-value"
        mock_kv.get_secret.assert_called_once_with("tenant-acme-apply-change-write-credential")


class TestPrepareStepUp:
    @pytest.mark.asyncio
    async def test_returns_none_when_active_grant_exists(self):
        from step_up import prepare_step_up

        active_grant = {"id": "grnt-001", "approved_by": "approver@client.com"}
        request = ChatRequest(tenant_id="tenant-a", session_id="s-001")
        policy = {
            "grant_type": "time_window",
            "pending_ttl_seconds": 300,
            "notification_channels": ["in_chat"],
            "grant_duration_seconds": 1800,
        }
        tenant_config = {}

        with patch("step_up.get_active_grant", new_callable=AsyncMock, return_value=active_grant):
            with patch("step_up.create_step_up_request", new_callable=AsyncMock) as mock_create:
                result = await prepare_step_up("bulk_close_tickets", {}, request, policy, tenant_config)

        assert result is None
        mock_create.assert_not_called()

    @pytest.mark.asyncio
    async def test_creates_request_when_no_active_grant(self):
        from step_up import prepare_step_up

        request = ChatRequest(tenant_id="tenant-a", session_id="s-001")
        policy = {
            "grant_type": "single_use",
            "pending_ttl_seconds": 900,
            "notification_channels": ["in_chat"],
        }
        tenant_config = {}
        mock_record = {"id": "sur-001", "status": "pending"}

        with patch("step_up.create_step_up_request", new_callable=AsyncMock, return_value=mock_record) as mock_create:
            result = await prepare_step_up("apply_change", {"change_id": "chg-001"}, request, policy, tenant_config)

        assert result == mock_record
        mock_create.assert_called_once()


class TestResolveStepUp:
    @pytest.mark.asyncio
    async def test_returns_approved_with_credential(self):
        from step_up import resolve_step_up

        step_up_req = {"id": "sur-001", "tool_name": "apply_change"}
        request = ChatRequest(tenant_id="tenant-a", session_id="s-001")
        policy = {"grant_type": "single_use"}
        decision = {"status": "approved", "approved_by": "approver@client.com"}

        with patch("step_up.await_step_up_decision", new_callable=AsyncMock, return_value=decision):
            with patch("step_up.fetch_tool_credential", new_callable=AsyncMock, return_value="cred-abc"):
                result = await resolve_step_up(step_up_req, request, policy)

        assert result.status == "approved"
        assert result.credential == "cred-abc"
        assert result.approved_by == "approver@client.com"

    @pytest.mark.asyncio
    async def test_returns_rejected_without_credential(self):
        from step_up import resolve_step_up

        step_up_req = {"id": "sur-002", "tool_name": "apply_change"}
        request = ChatRequest(tenant_id="tenant-a", session_id="s-001")
        policy = {"grant_type": "single_use"}
        decision = {"status": "rejected", "approved_by": "approver@client.com"}

        with patch("step_up.await_step_up_decision", new_callable=AsyncMock, return_value=decision):
            result = await resolve_step_up(step_up_req, request, policy)

        assert result.status == "rejected"
        assert result.credential is None

    @pytest.mark.asyncio
    async def test_returns_failed_when_kv_unreachable(self):
        from step_up import resolve_step_up

        step_up_req = {"id": "sur-003", "tool_name": "apply_change"}
        request = ChatRequest(tenant_id="tenant-a", session_id="s-001")
        policy = {"grant_type": "single_use"}
        decision = {"status": "approved", "approved_by": "approver@client.com"}

        with patch("step_up.await_step_up_decision", new_callable=AsyncMock, return_value=decision):
            with patch("step_up.fetch_tool_credential", new_callable=AsyncMock, side_effect=Exception("KV timeout")):
                with patch("step_up.mark_step_up_failed", new_callable=AsyncMock):
                    result = await resolve_step_up(step_up_req, request, policy)

        assert result.status == "failed"
        assert result.credential is None
