import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import agent_loop
from agent_loop import _stream_tool_call


def _parse_sse_events(chunks: list[str]) -> list[dict]:
    """Extract JSON payloads from SSE data lines."""
    events = []
    for chunk in chunks:
        for line in chunk.splitlines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


def _make_request(tenant_id="tenant-a", session_id="sess-1"):
    req = MagicMock()
    req.tenant_id = tenant_id
    req.session_id = session_id
    return req


class TestStreamToolCall:
    @pytest.mark.asyncio
    async def test_approval_required_emitted_before_poll(self):
        """approval_required SSE must be the first event yielded — before resolve_step_up blocks."""
        step_up_req = {
            "id": "sur-001",
            "context": {"change_id": "chg-001"},
            "expires_at": "2099-01-01T00:00:00+00:00",
        }
        step_up_result = MagicMock()
        step_up_result.status = "approved"
        step_up_result.request_id = "sur-001"
        step_up_result.approved_by = "approver@client.com"
        step_up_result.credential = "cred-abc"

        policy = {"self_approve": False}
        tenant_config = {"step_up_policy": {"apply_change": policy}}
        request = _make_request()

        with patch("agent_loop.prepare_step_up", new_callable=AsyncMock, return_value=step_up_req):
            with patch("agent_loop.resolve_step_up", new_callable=AsyncMock, return_value=step_up_result):
                with patch("agent_loop._call_agent", new_callable=AsyncMock, return_value={}):
                    chunks = []
                    async for chunk in _stream_tool_call("apply_change", {}, request, tenant_config):
                        chunks.append(chunk)

        events = _parse_sse_events(chunks)
        types = [e["type"] for e in events]
        assert types[0] == "approval_required", f"Expected approval_required first, got: {types}"
        assert "approval_granted" in types
        assert "agent_start" in types
        assert "agent_complete" in types

    @pytest.mark.asyncio
    async def test_approval_rejected_skips_tool_call(self):
        """When approval is rejected, agent_start and agent_complete must NOT be emitted."""
        step_up_req = {
            "id": "sur-002",
            "context": {},
            "expires_at": "2099-01-01T00:00:00+00:00",
        }
        step_up_result = MagicMock()
        step_up_result.status = "rejected"
        step_up_result.request_id = "sur-002"
        step_up_result.approved_by = "approver@client.com"

        policy = {"self_approve": False}
        tenant_config = {"step_up_policy": {"apply_change": policy}}
        request = _make_request()

        with patch("agent_loop.prepare_step_up", new_callable=AsyncMock, return_value=step_up_req):
            with patch("agent_loop.resolve_step_up", new_callable=AsyncMock, return_value=step_up_result):
                with patch("agent_loop._call_agent", new_callable=AsyncMock, return_value={}) as mock_call:
                    chunks = []
                    async for chunk in _stream_tool_call("apply_change", {}, request, tenant_config):
                        chunks.append(chunk)

        mock_call.assert_not_called()
        events = _parse_sse_events(chunks)
        types = [e["type"] for e in events]
        assert "approval_rejected" in types
        assert "agent_start" not in types
        assert "agent_complete" not in types

    @pytest.mark.asyncio
    async def test_no_step_up_for_unregistered_tool(self):
        """Tools not in step_up_policy bypass all approval events and dispatch immediately."""
        tenant_config = {"step_up_policy": {}}  # no entry for network_agent
        request = _make_request()

        with patch("agent_loop._call_agent", new_callable=AsyncMock, return_value={}) as mock_call:
            chunks = []
            async for chunk in _stream_tool_call("network_agent", {"device_host": "10.0.0.1"}, request, tenant_config):
                chunks.append(chunk)

        mock_call.assert_called_once()
        assert chunks == []  # no SSE events for non-gated tools

    @pytest.mark.asyncio
    async def test_active_grant_skips_approval_prompt(self):
        """When prepare_step_up returns None (active time-window grant), no approval events emitted."""
        step_up_result = MagicMock()
        step_up_result.status = "approved"
        step_up_result.request_id = "sur-003"
        step_up_result.approved_by = "user@client.com"
        step_up_result.credential = "cred-xyz"

        policy = {"self_approve": True}
        tenant_config = {"step_up_policy": {"apply_change": policy}}
        request = _make_request()

        with patch("agent_loop.prepare_step_up", new_callable=AsyncMock, return_value=None):
            with patch("agent_loop.fetch_tool_credential", new_callable=AsyncMock, return_value="cred-xyz"):
                with patch("agent_loop._call_agent", new_callable=AsyncMock, return_value={}):
                    chunks = []
                    async for chunk in _stream_tool_call("apply_change", {}, request, tenant_config):
                        chunks.append(chunk)

        events = _parse_sse_events(chunks)
        types = [e["type"] for e in events]
        assert "approval_required" not in types
        assert "approval_granted" not in types
        assert "agent_start" in types
        assert "agent_complete" in types

    @pytest.mark.asyncio
    async def test_keepalive_heartbeats_emitted_during_long_poll(self):
        """Keepalive comment lines emitted every _KEEPALIVE_INTERVAL seconds during approval wait."""
        step_up_req = {
            "id": "sur-004",
            "context": {},
            "expires_at": "2099-01-01T00:00:00+00:00",
        }
        step_up_result = MagicMock()
        step_up_result.status = "approved"
        step_up_result.request_id = "sur-004"
        step_up_result.approved_by = "approver@client.com"
        step_up_result.credential = "cred-keepalive"

        policy = {"self_approve": False}
        tenant_config = {"step_up_policy": {"apply_change": policy}}
        request = _make_request()

        # Patch _KEEPALIVE_INTERVAL to be very short so the test doesn't sleep for 30s
        original_interval = agent_loop._KEEPALIVE_INTERVAL
        agent_loop._KEEPALIVE_INTERVAL = 0.01

        async def slow_resolve(*args, **kwargs):
            await asyncio.sleep(0.05)  # long enough to trigger at least one keepalive
            return step_up_result

        try:
            with patch("agent_loop.prepare_step_up", new_callable=AsyncMock, return_value=step_up_req):
                with patch("agent_loop.resolve_step_up", side_effect=slow_resolve):
                    with patch("agent_loop._call_agent", new_callable=AsyncMock, return_value={}):
                        chunks = []
                        async for chunk in _stream_tool_call("apply_change", {}, request, tenant_config):
                            chunks.append(chunk)
        finally:
            agent_loop._KEEPALIVE_INTERVAL = original_interval

        keepalives = [c for c in chunks if c.startswith(": keepalive")]
        assert len(keepalives) >= 1, f"Expected at least one keepalive heartbeat, got chunks: {chunks}"
