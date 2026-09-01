import json

import pytest
from fastapi.testclient import TestClient

VALID_CLAIMS = {"tenant_id": "tenant-a", "user_identity": "alice@tenant-a.com"}


@pytest.fixture(autouse=True)
def _reset_rate_limit():
    from middleware.rate_limit import reset_rate_limits

    reset_rate_limits()
    yield
    reset_rate_limits()


def _parse_sse_events(body: bytes) -> list[dict]:
    events = []
    for frame in body.decode("utf-8").split("\n\n"):
        frame = frame.strip()
        if frame.startswith("data: "):
            events.append(json.loads(frame[len("data: "):]))
    return events


class TestRateLimitGate:
    def test_over_limit_returns_sse_rate_limited_error_before_coordinator_called(self, monkeypatch):
        import main
        from middleware.token_budget import BudgetStatus

        monkeypatch.setenv("GATEWAY_RATE_LIMIT_PER_MINUTE", "1")
        monkeypatch.setattr(main, "validate_ise_token", lambda request: VALID_CLAIMS)

        async def always_allowed(tenant_id):
            return BudgetStatus(allowed=True)

        monkeypatch.setattr(main, "_check_tenant_budget", always_allowed)

        called = {"count": 0}

        async def spy_stream(headers, body):
            called["count"] += 1
            yield b'data: {"type": "done", "tokens_used": 0, "session_id": "s1"}\n\n'

        monkeypatch.setattr(main, "_stream_from_coordinator", spy_stream)

        client = TestClient(main.app)
        req = {"session_id": "s1", "tenant_id": "tenant-a", "messages": []}
        headers = {"Authorization": "Bearer whatever"}

        first = client.post("/chat/stream", json=req, headers=headers)
        assert first.status_code == 200
        assert called["count"] == 1

        second = client.post("/chat/stream", json=req, headers=headers)

        # SSE-compatible payload: HTTP 200 + text/event-stream body carrying the
        # error event, matching how the UI's fetch-based client only parses the
        # body when response.ok is true.
        assert second.status_code == 200
        assert second.headers["content-type"].startswith("text/event-stream")
        events = _parse_sse_events(second.content)
        assert len(events) == 1
        assert events[0]["type"] == "error"
        assert events[0]["code"] == "rate_limited"
        assert isinstance(events[0]["message"], str) and events[0]["message"]
        # The Coordinator must never have been reached for the rejected request.
        assert called["count"] == 1


class TestBudgetGate:
    def test_budget_exceeded_returns_sse_error_before_coordinator_called(self, monkeypatch):
        import main
        from middleware.token_budget import BudgetStatus

        monkeypatch.setattr(main, "validate_ise_token", lambda request: VALID_CLAIMS)

        async def rejected(tenant_id):
            return BudgetStatus(allowed=False, reason="daily_budget_exceeded")

        monkeypatch.setattr(main, "_check_tenant_budget", rejected)

        called = {"count": 0}

        async def spy_stream(headers, body):
            called["count"] += 1
            yield b""

        monkeypatch.setattr(main, "_stream_from_coordinator", spy_stream)

        client = TestClient(main.app)
        response = client.post(
            "/chat/stream",
            json={"session_id": "s1", "tenant_id": "tenant-a", "messages": []},
            headers={"Authorization": "Bearer whatever"},
        )

        assert response.status_code == 200
        events = _parse_sse_events(response.content)
        assert len(events) == 1
        assert events[0]["type"] == "error"
        assert events[0]["code"] == "budget_exceeded"
        assert called["count"] == 0

    def test_budget_lookup_failure_fails_closed_at_the_route(self, monkeypatch):
        """
        End-to-end: if the Cosmos DB lookup backing the budget check blows up,
        the route must reject the request (fail closed), not silently proxy it
        through to the Coordinator.
        """
        import main

        monkeypatch.setattr(main, "validate_ise_token", lambda request: VALID_CLAIMS)
        monkeypatch.setattr(main, "tenant_config_container", _RaisingContainer())

        called = {"count": 0}

        async def spy_stream(headers, body):
            called["count"] += 1
            yield b""

        monkeypatch.setattr(main, "_stream_from_coordinator", spy_stream)

        client = TestClient(main.app)
        response = client.post(
            "/chat/stream",
            json={"session_id": "s1", "tenant_id": "tenant-a", "messages": []},
            headers={"Authorization": "Bearer whatever"},
        )

        assert response.status_code == 200
        events = _parse_sse_events(response.content)
        assert events[0]["code"] == "budget_exceeded"
        assert called["count"] == 0


class _RaisingContainer:
    async def read_item(self, item, partition_key):
        raise RuntimeError("Cosmos DB unreachable")
