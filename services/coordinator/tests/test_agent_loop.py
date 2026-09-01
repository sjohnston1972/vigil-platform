import asyncio
import json
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import agent_loop
from agent_loop import _sse, _stream_tool_call
from model_client import ModelTurn, ToolCall


def _parse_sse_events(chunks: list[str]) -> list[dict]:
    """Extract JSON payloads from SSE data lines."""
    events = []
    for chunk in chunks:
        for line in chunk.splitlines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


def _make_request(tenant_id="tenant-a", session_id="sess-1", messages=None):
    req = MagicMock()
    req.tenant_id = tenant_id
    req.session_id = session_id
    req.messages = messages if messages is not None else [_make_chat_message("user", "hello")]
    return req


def _make_chat_message(role: str, content: str):
    """Stand-in for main.ChatMessage — only .model_dump() is needed by _stream_generator."""
    msg = MagicMock()
    msg.model_dump.return_value = {"role": role, "content": content}
    return msg


class FakeModelClient:
    """
    Scripted ModelClient for tests — each call to run_turn() consumes the next
    scripted (text_deltas, ModelTurn) pair. Records every call's kwargs so tests
    can assert on what was sent to the model (messages, tools, max_tokens).
    """

    def __init__(self, turns):
        self._turns = list(turns)
        self.calls = []

    async def run_turn(self, messages, tools, max_tokens, system=None):
        # Snapshot — the caller's `messages` list is mutated (appended to) after this
        # call returns, so recording the reference would make every recorded call
        # show the same final state.
        self.calls.append(
            {"messages": list(messages), "tools": list(tools), "max_tokens": max_tokens, "system": system}
        )
        text_deltas, turn = self._turns.pop(0)
        for delta in text_deltas:
            yield {"type": "text_delta", "text": delta}
        yield {"type": "turn_complete", "turn": turn}


class TestStreamGeneratorTextOnly:
    """#13 — text-only reply path: session_start -> token* -> done."""

    @pytest.mark.asyncio
    async def test_text_only_reply_streams_session_start_tokens_done(self):
        turn = ModelTurn(
            text="Hello there",
            tool_calls=[],
            stop_reason="end_turn",
            input_tokens=10,
            output_tokens=5,
        )
        fake_client = FakeModelClient([(["Hello", " there"], turn)])
        request = _make_request()

        with patch("agent_loop._get_model_client", return_value=fake_client):
            chunks = []
            async for chunk in agent_loop._stream_generator(request, {}):
                chunks.append(chunk)

        events = _parse_sse_events(chunks)
        types = [e["type"] for e in events]

        assert types[0] == "session_start"
        assert events[0]["session_id"] == "sess-1"
        assert events[0]["tenant_id"] == "tenant-a"
        assert types.count("token") == 2
        assert "".join(e["content"] for e in events if e["type"] == "token") == "Hello there"
        assert types[-1] == "done"
        assert events[-1]["tokens_used"] == 15
        assert events[-1]["session_id"] == "sess-1"

    @pytest.mark.asyncio
    async def test_tool_definitions_sent_to_model_client(self):
        """Every /chat/stream call registers the specialist-agent tool schemas with Claude."""
        turn = ModelTurn(text="ok", tool_calls=[], stop_reason="end_turn", input_tokens=1, output_tokens=1)
        fake_client = FakeModelClient([([], turn)])
        request = _make_request()

        with patch("agent_loop._get_model_client", return_value=fake_client):
            async for _ in agent_loop._stream_generator(request, {}):
                pass

        sent_tool_names = {t["name"] for t in fake_client.calls[0]["tools"]}
        assert "network_agent" in sent_tool_names
        assert "apply_change" in sent_tool_names


class TestStreamGeneratorToolDispatch:
    """#14 — parallel non-gated tool dispatch via asyncio.as_completed()."""

    @pytest.mark.asyncio
    async def test_parallel_tools_run_concurrently_not_sequentially(self):
        """
        Two independent non-gated tools each take DELAY seconds. If dispatched
        sequentially the round takes ~2*DELAY; dispatched concurrently (the
        asyncio.as_completed() requirement) it takes ~DELAY. This measures wall
        time rather than asserting concurrency happened.
        """
        DELAY = 0.15
        tool_call_1 = ToolCall(id="tu_1", name="network_agent", input={"device_host": "10.0.0.1", "query_type": "interfaces"})
        tool_call_2 = ToolCall(id="tu_2", name="enrichment_agent", input={"product_id": "CVE-2026-1"})

        turn_with_tools = ModelTurn(
            text="", tool_calls=[tool_call_1, tool_call_2], stop_reason="tool_use",
            input_tokens=20, output_tokens=10,
        )
        final_turn = ModelTurn(text="Done", tool_calls=[], stop_reason="end_turn", input_tokens=5, output_tokens=5)
        fake_client = FakeModelClient([([], turn_with_tools), (["Done"], final_turn)])
        request = _make_request()

        async def slow_call_agent(tool_name, tool_input, req, credential=None):
            await asyncio.sleep(DELAY)
            return {"ok": True, "tool": tool_name}

        with patch("agent_loop._get_model_client", return_value=fake_client):
            with patch("agent_loop._call_agent", side_effect=slow_call_agent):
                start = time.monotonic()
                chunks = []
                async for chunk in agent_loop._stream_generator(request, {}):
                    chunks.append(chunk)
                elapsed = time.monotonic() - start

        assert elapsed < DELAY * 1.8, f"expected concurrent dispatch (~{DELAY}s), took {elapsed:.3f}s"

        events = _parse_sse_events(chunks)
        complete_events = [e for e in events if e["type"] == "agent_complete"]
        assert {e["agent"] for e in complete_events} == {"network_agent", "enrichment_agent"}

    @pytest.mark.asyncio
    async def test_faster_agent_completes_first(self):
        """agent_complete for the faster tool is emitted before the slower tool's."""
        tool_call_slow = ToolCall(id="tu_1", name="network_agent", input={"device_host": "10.0.0.1", "query_type": "interfaces"})
        tool_call_fast = ToolCall(id="tu_2", name="enrichment_agent", input={"product_id": "CVE-2026-1"})

        turn_with_tools = ModelTurn(
            text="", tool_calls=[tool_call_slow, tool_call_fast], stop_reason="tool_use",
            input_tokens=20, output_tokens=10,
        )
        final_turn = ModelTurn(text="Done", tool_calls=[], stop_reason="end_turn", input_tokens=5, output_tokens=5)
        fake_client = FakeModelClient([([], turn_with_tools), (["Done"], final_turn)])
        request = _make_request()

        async def variable_delay_call_agent(tool_name, tool_input, req, credential=None):
            await asyncio.sleep(0.15 if tool_name == "network_agent" else 0.02)
            return {"ok": True}

        with patch("agent_loop._get_model_client", return_value=fake_client):
            with patch("agent_loop._call_agent", side_effect=variable_delay_call_agent):
                chunks = []
                async for chunk in agent_loop._stream_generator(request, {}):
                    chunks.append(chunk)

        events = _parse_sse_events(chunks)
        complete_order = [e["agent"] for e in events if e["type"] == "agent_complete"]
        assert complete_order == ["enrichment_agent", "network_agent"]

    @pytest.mark.asyncio
    async def test_failing_agent_emits_agent_error_and_loop_continues(self):
        """A failing non-gated agent emits agent_error; the coordinator still reaches done."""
        tool_call_ok = ToolCall(id="tu_1", name="rag_agent", input={"query": "is this compliant"})
        tool_call_fail = ToolCall(id="tu_2", name="itsm_agent", input={"action": "query", "ticket_id": "VIGIL-1"})

        turn_with_tools = ModelTurn(
            text="", tool_calls=[tool_call_ok, tool_call_fail], stop_reason="tool_use",
            input_tokens=20, output_tokens=10,
        )
        final_turn = ModelTurn(text="Partial results", tool_calls=[], stop_reason="end_turn", input_tokens=5, output_tokens=5)
        fake_client = FakeModelClient([([], turn_with_tools), (["Partial results"], final_turn)])
        request = _make_request()

        async def flaky_call_agent(tool_name, tool_input, req, credential=None):
            if tool_name == "itsm_agent":
                raise RuntimeError("jira unreachable")
            return {"ok": True}

        with patch("agent_loop._get_model_client", return_value=fake_client):
            with patch("agent_loop._call_agent", side_effect=flaky_call_agent):
                chunks = []
                async for chunk in agent_loop._stream_generator(request, {}):
                    chunks.append(chunk)

        events = _parse_sse_events(chunks)
        types = [e["type"] for e in events]
        assert "agent_error" in types
        error_event = next(e for e in events if e["type"] == "agent_error")
        assert error_event["agent"] == "itsm_agent"
        assert error_event["error"] == "jira unreachable"
        assert "agent_complete" in types  # rag_agent still succeeded
        assert types[-1] == "done"

        # The failing tool's result must still feed a tool_result back to Claude —
        # the second model call proves the loop continued past the failure.
        assert len(fake_client.calls) == 2
        second_turn_messages = fake_client.calls[1]["messages"]
        tool_results = second_turn_messages[-1]["content"]
        assert any(r.get("is_error") for r in tool_results)

    @pytest.mark.asyncio
    async def test_gated_tool_reuses_stream_tool_call(self):
        """A step-up gated tool call in the tool round goes through _stream_tool_call."""
        tool_call = ToolCall(id="tu_1", name="apply_change", input={"change_id": "chg-001"})
        turn_with_tool = ModelTurn(
            text="", tool_calls=[tool_call], stop_reason="tool_use", input_tokens=20, output_tokens=10,
        )
        final_turn = ModelTurn(text="Applied", tool_calls=[], stop_reason="end_turn", input_tokens=5, output_tokens=5)
        fake_client = FakeModelClient([([], turn_with_tool), (["Applied"], final_turn)])
        request = _make_request()
        tenant_config = {"step_up_policy": {"apply_change": {"self_approve": False}}}

        async def fake_stream_tool_call(tool_name, tool_input, req, cfg, result_holder=None):
            assert tool_name == "apply_change"
            if result_holder is not None:
                result_holder["data"] = {"status": "applied"}
            yield _sse({"type": "approval_granted", "request_id": "sur-1", "tool": tool_name, "approved_by": "approver@client.com"})

        with patch("agent_loop._get_model_client", return_value=fake_client):
            with patch("agent_loop._stream_tool_call", side_effect=fake_stream_tool_call):
                chunks = []
                async for chunk in agent_loop._stream_generator(request, tenant_config):
                    chunks.append(chunk)

        events = _parse_sse_events(chunks)
        types = [e["type"] for e in events]
        assert "approval_granted" in types
        # Gated tools bypass the concurrent non-gated path entirely.
        assert "agent_start" not in types


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
