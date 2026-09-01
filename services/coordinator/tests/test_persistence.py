"""
#15 — Persist conversation + audit to Cosmos and emit an accurate `done`.

Cosmos DB is not reachable from this environment, so these tests verify
_persist_conversation_and_audit against mocked `main.conversations_container` /
`main.audit_logs_container` (same convention as tests/test_sessions.py), and verify
the end-to-end /chat/stream event contract (done.tokens_used, error codes) against
a scripted FakeModelClient. Nothing here talks to a live Cosmos account.
"""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from azure.cosmos.exceptions import CosmosResourceNotFoundError

import agent_loop
from model_client import ModelBudgetExceededError, ModelRateLimitedError, ModelTurn, ToolCall


def _parse_sse_events(chunks: list[str]) -> list[dict]:
    """Extract JSON payloads from SSE data lines."""
    events = []
    for chunk in chunks:
        for line in chunk.splitlines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


def _make_chat_message(role: str, content: str):
    """Stand-in for main.ChatMessage — only .model_dump() is needed by _stream_generator."""
    msg = MagicMock()
    msg.model_dump.return_value = {"role": role, "content": content}
    return msg


def _make_request(tenant_id="tenant-a", session_id="sess-1", messages=None):
    req = MagicMock()
    req.tenant_id = tenant_id
    req.session_id = session_id
    req.messages = messages if messages is not None else [_make_chat_message("user", "hello")]
    return req


class FakeModelClient:
    """Scripted ModelClient — see tests/test_agent_loop.py for the full docstring."""

    def __init__(self, turns):
        self._turns = list(turns)
        self.calls = []

    async def run_turn(self, messages, tools, max_tokens, system=None):
        self.calls.append(
            {"messages": list(messages), "tools": list(tools), "max_tokens": max_tokens, "system": system}
        )
        text_deltas, turn = self._turns.pop(0)
        for delta in text_deltas:
            yield {"type": "text_delta", "text": delta}
        yield {"type": "turn_complete", "turn": turn}


class TestPersistConversationAndAudit:
    """Exercises the real persistence function against a mocked Cosmos client."""

    @pytest.mark.asyncio
    async def test_creates_new_conversation_and_audit_entry(self):
        request = _make_request(tenant_id="tenant-a", session_id="sess-new")
        messages = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": [{"type": "text", "text": "hi"}]}]

        mock_conversations = MagicMock()
        mock_conversations.read_item = AsyncMock(side_effect=CosmosResourceNotFoundError(404, "not found"))
        mock_conversations.upsert_item = AsyncMock()
        mock_audit = MagicMock()
        mock_audit.create_item = AsyncMock()

        with patch("main.conversations_container", mock_conversations):
            with patch("main.audit_logs_container", mock_audit):
                await agent_loop._persist_conversation_and_audit(
                    request, messages, ["network_agent", "rag_agent"], tokens_used=123
                )

        mock_conversations.upsert_item.assert_called_once()
        upserted = mock_conversations.upsert_item.call_args.kwargs["body"]
        assert upserted["id"] == "sess-new"
        assert upserted["tenant_id"] == "tenant-a"
        assert upserted["messages"] == messages
        assert upserted["agents"] == ["network_agent", "rag_agent"]
        assert "updated_at" in upserted
        assert "created_at" in upserted

        mock_audit.create_item.assert_called_once()
        audit_body = mock_audit.create_item.call_args.kwargs["body"]
        assert audit_body["tenant_id"] == "tenant-a"
        assert audit_body["session_id"] == "sess-new"
        assert audit_body["tokens_used"] == 123
        assert audit_body["agents"] == ["network_agent", "rag_agent"]

    @pytest.mark.asyncio
    async def test_preserves_existing_title_and_merges_agents(self):
        request = _make_request(tenant_id="tenant-a", session_id="sess-existing")
        existing_doc = {
            "id": "sess-existing",
            "tenant_id": "tenant-a",
            "title": "Renamed by user",
            "created_at": "2026-01-01T00:00:00+00:00",
            "agents": ["network_agent"],
        }

        mock_conversations = MagicMock()
        mock_conversations.read_item = AsyncMock(return_value=dict(existing_doc))
        mock_conversations.upsert_item = AsyncMock()
        mock_audit = MagicMock()
        mock_audit.create_item = AsyncMock()

        with patch("main.conversations_container", mock_conversations):
            with patch("main.audit_logs_container", mock_audit):
                await agent_loop._persist_conversation_and_audit(
                    request, [{"role": "user", "content": "more"}], ["rag_agent"], tokens_used=10
                )

        upserted = mock_conversations.upsert_item.call_args.kwargs["body"]
        assert upserted["title"] == "Renamed by user"
        assert upserted["agents"] == ["network_agent", "rag_agent"]

    @pytest.mark.asyncio
    async def test_upsert_and_audit_write_happen_exactly_once(self):
        request = _make_request(tenant_id="tenant-a", session_id="sess-once")
        mock_conversations = MagicMock()
        mock_conversations.read_item = AsyncMock(side_effect=CosmosResourceNotFoundError(404, "not found"))
        mock_conversations.upsert_item = AsyncMock()
        mock_audit = MagicMock()
        mock_audit.create_item = AsyncMock()

        with patch("main.conversations_container", mock_conversations):
            with patch("main.audit_logs_container", mock_audit):
                await agent_loop._persist_conversation_and_audit(
                    request, [{"role": "user", "content": "hi"}], [], tokens_used=1
                )

        mock_conversations.upsert_item.assert_awaited_once()
        mock_audit.create_item.assert_awaited_once()


class TestStreamGeneratorPersistsBeforeDone:
    """End-to-end: /chat/stream persists to Cosmos before emitting `done`."""

    @pytest.mark.asyncio
    async def test_persistence_called_once_before_done(self):
        turn = ModelTurn(text="Hello", tool_calls=[], stop_reason="end_turn", input_tokens=7, output_tokens=3)
        fake_client = FakeModelClient([(["Hello"], turn)])
        request = _make_request()

        calls_order = []

        async def fake_persist(req, messages, agents_invoked, tokens_used):
            calls_order.append(("persist", tokens_used))

        with patch("agent_loop._get_model_client", return_value=fake_client):
            with patch("agent_loop._persist_conversation_and_audit", side_effect=fake_persist) as mock_persist:
                chunks = []
                async for chunk in agent_loop._stream_generator(request, {}):
                    chunks.append(chunk)
                    events = _parse_sse_events([chunk])
                    if events and events[0]["type"] == "done":
                        calls_order.append(("done", events[0]["tokens_used"]))

        mock_persist.assert_awaited_once()
        assert calls_order == [("persist", 10), ("done", 10)]

    @pytest.mark.asyncio
    async def test_done_tokens_used_reflects_model_usage_across_tool_rounds(self):
        tool_call = ToolCall(id="tu_1", name="rag_agent", input={"query": "x"})
        turn_with_tool = ModelTurn(text="", tool_calls=[tool_call], stop_reason="tool_use", input_tokens=20, output_tokens=8)
        final_turn = ModelTurn(text="Answer", tool_calls=[], stop_reason="end_turn", input_tokens=15, output_tokens=6)
        fake_client = FakeModelClient([([], turn_with_tool), (["Answer"], final_turn)])
        request = _make_request()

        with patch("agent_loop._get_model_client", return_value=fake_client):
            with patch("agent_loop._call_agent", new_callable=AsyncMock, return_value={"ok": True}):
                with patch("agent_loop._persist_conversation_and_audit", new_callable=AsyncMock):
                    chunks = []
                    async for chunk in agent_loop._stream_generator(request, {}):
                        chunks.append(chunk)

        events = _parse_sse_events(chunks)
        done_event = next(e for e in events if e["type"] == "done")
        assert done_event["tokens_used"] == (20 + 8) + (15 + 6)

    @pytest.mark.asyncio
    async def test_done_never_emitted_when_persistence_fails(self):
        """Cosmos write failure must produce a coordinator_unavailable error, never `done`."""
        turn = ModelTurn(text="Hello", tool_calls=[], stop_reason="end_turn", input_tokens=1, output_tokens=1)
        fake_client = FakeModelClient([(["Hello"], turn)])
        request = _make_request()

        with patch("agent_loop._get_model_client", return_value=fake_client):
            with patch("agent_loop._persist_conversation_and_audit", new_callable=AsyncMock, side_effect=Exception("cosmos write failed")):
                chunks = []
                async for chunk in agent_loop._stream_generator(request, {}):
                    chunks.append(chunk)

        events = _parse_sse_events(chunks)
        types = [e["type"] for e in events]
        assert "done" not in types
        assert types[-1] == "error"
        assert events[-1]["code"] == "coordinator_unavailable"


class TestStreamGeneratorErrorCodes:
    """#15 — fatal errors emit `error` with a code: budget_exceeded | rate_limited | coordinator_unavailable."""

    @pytest.mark.asyncio
    async def test_budget_exceeded_when_tenant_budget_exhausted(self):
        request = _make_request()
        tenant_config = {"token_budget_remaining": 0}

        with patch("agent_loop._get_model_client", return_value=FakeModelClient([])):
            chunks = []
            async for chunk in agent_loop._stream_generator(request, tenant_config):
                chunks.append(chunk)

        events = _parse_sse_events(chunks)
        assert events[-1]["type"] == "error"
        assert events[-1]["code"] == "budget_exceeded"

    @pytest.mark.asyncio
    async def test_rate_limited_error_code(self):
        request = _make_request()

        class RateLimitedModelClient:
            async def run_turn(self, messages, tools, max_tokens, system=None):
                raise ModelRateLimitedError("429 from provider")
                yield  # pragma: no cover — makes this an async generator

        with patch("agent_loop._get_model_client", return_value=RateLimitedModelClient()):
            chunks = []
            async for chunk in agent_loop._stream_generator(request, {}):
                chunks.append(chunk)

        events = _parse_sse_events(chunks)
        assert events[-1]["type"] == "error"
        assert events[-1]["code"] == "rate_limited"

    @pytest.mark.asyncio
    async def test_coordinator_unavailable_on_unexpected_error(self):
        request = _make_request()

        class BrokenModelClient:
            async def run_turn(self, messages, tools, max_tokens, system=None):
                raise RuntimeError("boom")
                yield  # pragma: no cover

        with patch("agent_loop._get_model_client", return_value=BrokenModelClient()):
            chunks = []
            async for chunk in agent_loop._stream_generator(request, {}):
                chunks.append(chunk)

        events = _parse_sse_events(chunks)
        assert events[-1]["type"] == "error"
        assert events[-1]["code"] == "coordinator_unavailable"


class TestGetSessionsReflectsChatStreamPersistence:
    """
    AC: "After a stream, GET /sessions returns the updated session."

    Runs the doc built by the real _persist_conversation_and_audit straight through
    GET /sessions' query_items mock — proves the persisted document shape satisfies
    the SessionSummary response model the endpoint validates against.
    """

    @pytest.mark.asyncio
    async def test_session_upserted_by_stream_is_returned_by_get_sessions(self):
        from fastapi.testclient import TestClient

        request = _make_request(tenant_id="tenant-a", session_id="sess-after-stream")
        mock_conversations = MagicMock()
        mock_conversations.read_item = AsyncMock(side_effect=CosmosResourceNotFoundError(404, "not found"))
        persisted_doc = {}

        async def fake_upsert(body):
            persisted_doc.update(body)

        mock_conversations.upsert_item = AsyncMock(side_effect=fake_upsert)
        mock_audit = MagicMock()
        mock_audit.create_item = AsyncMock()

        with patch("main.conversations_container", mock_conversations):
            with patch("main.audit_logs_container", mock_audit):
                await agent_loop._persist_conversation_and_audit(
                    request,
                    [{"role": "user", "content": "audit the perimeter firewall"}],
                    ["network_agent", "enrichment_agent"],
                    tokens_used=42,
                )

        async def _async_iter(items):
            for item in items:
                yield item

        with patch("main.conversations_container") as list_mock_container:
            list_mock_container.query_items.return_value = _async_iter([persisted_doc])
            from main import app
            response = TestClient(app).get("/sessions", headers={"X-Tenant-Id": "tenant-a"})

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["id"] == "sess-after-stream"
        assert data[0]["agents"] == ["enrichment_agent", "network_agent"]
