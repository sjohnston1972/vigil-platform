import asyncio
import json
import time

import httpx
import pytest


VALID_CLAIMS = {"tenant_id": "tenant-a", "user_identity": "alice@tenant-a.com"}


def _mock_valid_auth(monkeypatch, main):
    monkeypatch.setattr(main, "validate_ise_token", lambda request: VALID_CLAIMS)


@pytest.fixture(autouse=True)
def _bypass_rate_limit_and_budget_gates(monkeypatch):
    """
    This test module exercises the streaming proxy mechanics (#17), not the
    pre-flight rate-limit/budget gates added on top of it (#18, covered in
    tests/test_rate_limit.py and tests/test_chat_stream_gating.py). Reset the
    in-memory rate limiter and stub the budget check to "allowed" by default so
    those gates don't interfere with these tests; individual tests can still
    override them.
    """
    import main
    from middleware.rate_limit import reset_rate_limits
    from middleware.token_budget import BudgetStatus

    reset_rate_limits()

    async def always_allowed(tenant_id):
        return BudgetStatus(allowed=True)

    monkeypatch.setattr(main, "_check_tenant_budget", always_allowed)
    yield
    reset_rate_limits()


async def _drive_asgi(app, scope, body: bytes = b""):
    """
    Manually drive an ASGI app end-to-end, recording a (monotonic_time, message)
    pair for every message the app sends. Unlike httpx's ASGITransport (which
    runs the whole app to completion and only exposes the fully-collected body
    afterwards — no use for timing assertions), this records each
    http.response.body message at the exact moment Starlette emits it, which is
    exactly when the underlying generator produced that chunk. This proves
    whether the app relays chunks as they are produced or waits and batches them.
    """
    sent = []
    request_sent = False

    async def receive():
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        # Starlette's StreamingResponse races a "listen for disconnect" task
        # against the streaming generator. Never resolving here (until the
        # generator itself finishes and this task gets cancelled) simulates a
        # client that stays connected for the whole response.
        await asyncio.Future()

    async def send(message):
        sent.append((time.monotonic(), message))

    await app(scope, receive, send)
    return sent


def _post_scope(path: str, body: bytes, extra_headers: list[tuple[bytes, bytes]] | None = None) -> dict:
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
    ] + (extra_headers or [])
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers,
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 123),
        "root_path": "",
    }


class TestNonBufferingStream:
    @pytest.mark.asyncio
    async def test_chunks_arrive_incrementally_not_all_at_once(self, monkeypatch):
        """
        Stub upstream that emits four SSE chunks with a 0.2s delay between each.
        If the Gateway buffered the response, all four chunks would arrive to the
        client at once, roughly 0.6s after the request — bunched together with
        ~0 gap between them. A non-buffering proxy delivers each chunk to the
        client as soon as it is produced, so arrival times are spread out and the
        first chunk arrives almost immediately.
        """
        import main

        _mock_valid_auth(monkeypatch, main)

        delay = 0.2

        async def fake_stream(headers, body):
            yield b'data: {"type": "session_start", "session_id": "s1", "tenant_id": "tenant-a"}\n\n'
            await asyncio.sleep(delay)
            yield b'data: {"type": "token", "content": "Hello"}\n\n'
            await asyncio.sleep(delay)
            yield b'data: {"type": "token", "content": " world"}\n\n'
            await asyncio.sleep(delay)
            yield b'data: {"type": "done", "tokens_used": 12, "session_id": "s1"}\n\n'

        monkeypatch.setattr(main, "_stream_from_coordinator", fake_stream)

        body = json.dumps({"session_id": "s1", "tenant_id": "client-supplied-ignored", "messages": []}).encode()
        scope = _post_scope("/chat/stream", body, extra_headers=[(b"authorization", b"Bearer whatever")])

        start = time.monotonic()
        sent = await _drive_asgi(main.app, scope, body)

        body_events = [(t - start, m) for (t, m) in sent if m["type"] == "http.response.body" and m.get("body")]
        arrival_times = [t for t, _ in body_events]

        assert len(arrival_times) == 4
        # First chunk must not wait for the later, delayed chunks.
        assert arrival_times[0] < delay / 2
        # Total elapsed time reflects all three inter-chunk delays having happened
        # incrementally (not one big wait followed by an instant flush).
        assert arrival_times[-1] > 3 * delay * 0.8
        gaps = [arrival_times[i] - arrival_times[i - 1] for i in range(1, len(arrival_times))]
        for gap in gaps:
            assert gap > delay * 0.5, f"gap {gap} too small — response looks buffered, not streamed"

    @pytest.mark.asyncio
    async def test_response_headers_disable_intermediary_buffering(self, monkeypatch):
        import main

        _mock_valid_auth(monkeypatch, main)

        async def fake_stream(headers, body):
            yield b'data: {"type": "done", "tokens_used": 0, "session_id": "s1"}\n\n'

        monkeypatch.setattr(main, "_stream_from_coordinator", fake_stream)

        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/chat/stream",
                json={"session_id": "s1", "messages": []},
                headers={"Authorization": "Bearer whatever"},
            )
        assert response.headers["cache-control"] == "no-cache"
        assert response.headers["x-accel-buffering"] == "no"


class TestIdentityInjection:
    @pytest.mark.asyncio
    async def test_identity_headers_come_from_validated_claims_not_client_body(self, monkeypatch):
        import main

        _mock_valid_auth(monkeypatch, main)

        captured = {}

        async def fake_stream(headers, body):
            captured["headers"] = headers
            captured["body"] = body
            yield b'data: {"type": "done", "tokens_used": 0, "session_id": "s1"}\n\n'

        monkeypatch.setattr(main, "_stream_from_coordinator", fake_stream)

        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post(
                "/chat/stream",
                json={
                    "session_id": "s1",
                    "tenant_id": "attacker-supplied-tenant",
                    "messages": [{"role": "user", "content": "hi"}],
                },
                headers={"Authorization": "Bearer whatever"},
            )

        assert captured["headers"]["X-Tenant-Id"] == "tenant-a"
        assert captured["headers"]["X-User-Identity"] == "alice@tenant-a.com"
        # The client-supplied tenant_id in the body must be overwritten with the
        # server-validated tenant_id — never trusted from the client.
        assert captured["body"]["tenant_id"] == "tenant-a"

    @pytest.mark.asyncio
    async def test_missing_auth_rejected_before_any_upstream_call(self, monkeypatch):
        import main

        called = {"count": 0}

        async def fake_stream(headers, body):
            called["count"] += 1
            yield b""

        monkeypatch.setattr(main, "_stream_from_coordinator", fake_stream)

        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/chat/stream",
                json={"session_id": "s1", "messages": []},
            )
        assert response.status_code == 401
        assert called["count"] == 0


class TestCoordinatorUnavailable:
    @pytest.mark.asyncio
    async def test_connection_failure_yields_error_sse_event(self):
        import main

        headers = {"X-Tenant-Id": "tenant-a", "X-User-Identity": "alice@tenant-a.com", "Content-Type": "application/json"}
        original_url = main.COORDINATOR_URL
        main.COORDINATOR_URL = "http://127.0.0.1:1"  # nothing listens here
        try:
            chunks = [chunk async for chunk in main._stream_from_coordinator(headers, {"session_id": "s1", "tenant_id": "tenant-a", "messages": []})]
        finally:
            main.COORDINATOR_URL = original_url

        assert len(chunks) == 1
        payload = json.loads(chunks[0].decode("utf-8").removeprefix("data: ").strip())
        assert payload == {
            "type": "error",
            "code": "coordinator_unavailable",
            "message": "Service temporarily unavailable",
        }
