# VIGIL — Parallel Coordinator Execution + SSE Streaming

**Date:** 2026-03-16
**Status:** Approved
**Scope:** `services/coordinator`, `services/gateway`, `services/ui`

---

## Overview

Two improvements to the VIGIL platform's request/response behaviour:

1. **Parallel coordinator execution** — when Claude determines multiple specialist agents can be called independently, fan them out concurrently with `asyncio.as_completed()` so per-agent completion events are emitted in real time as each agent finishes.
2. **SSE streaming** — expose a streaming endpoint so the UI receives live agent progress updates and token-by-token response generation rather than a single buffered response.

The result: the user sees which agents are running (in parallel where applicable), their individual completion times as they happen, and the final response streaming in — all in real time.

---

## Architecture

### Approach

SSE is owned by the Coordinator and proxied by the Gateway. The Coordinator exposes a streaming endpoint that yields SSE events throughout the agentic loop. The Gateway validates auth/rate limits/budget before opening the proxied stream. The UI maintains a persistent sidebar showing agent activity across all messages in the session.

### Why this approach

- Keeps SSE concern in the Coordinator where all orchestration logic lives
- Gateway remains a pure auth/rate-limit/budget proxy — no orchestration awareness required
- `asyncio.as_completed()` parallelism requires no external dependencies — pure Python async
- SSE over `fetch` with `ReadableStream` is sufficient for a one-way server-to-client stream; WebSockets would add complexity for no benefit given the request/stream-response interaction pattern

---

## Components

### 1. Coordinator — `services/coordinator`

#### New endpoint

```
POST /chat/stream
Content-Type: application/json
Response: text/event-stream
```

The existing `POST /chat` endpoint is kept as a non-streaming fallback.

#### Agentic loop

```
1. Load conversation history from Cosmos DB (partition key: tenant_id)
2. Load tenant config from Cosmos DB — get max_tokens for this request type
3. Emit session_start event
4. Call Claude API (streaming=True, max_tokens from tenant config) with tool definitions
5. Collect tool_use blocks from stream
6. For each set of tool calls returned in a single Claude response:
   a. Create a coroutine per tool call
   b. Emit agent_start for each, in immediate succession, before any begin executing
   c. Use asyncio.as_completed() to iterate coroutines as they finish
   d. For each completed coroutine: emit agent_complete or agent_error immediately
7. Feed all tool results back to Claude (streaming=True, max_tokens from tenant config)
8. If Claude returns more tool calls, repeat from step 5
9. When Claude returns final text response, emit token events per token
10. Write updated conversation + audit log to Cosmos DB (before emitting done)
11. Emit done event with total tokens_used and session_id
```

**Parallelism detail:** When Claude returns multiple `tool_use` blocks in a single response they are independent by definition. Each becomes a coroutine. `asyncio.as_completed()` wraps them and yields futures as they finish — each completion triggers an immediate `agent_complete` or `agent_error` event. This gives real-time per-agent status rather than a batch of events after the slowest agent completes.

**`max_tokens`:** Loaded from tenant config in Cosmos DB at step 2. Applied to every Claude call in the loop. Different values apply to conversational responses versus audit reports — the tenant config document specifies both. This enforces per-tenant token budget caps at the Claude call level.

**Cosmos DB write order:** The conversation and audit log are written to Cosmos DB at step 10, before `done` is emitted at step 11. This ensures audit integrity — if the write fails, `done` is never emitted and the client receives a stream-level error. A completed interaction always has a corresponding audit log entry.

#### FastAPI streaming response

```python
from fastapi.responses import StreamingResponse

@app.post("/chat/stream")
async def chat_stream(request: ChatRequest) -> StreamingResponse:
    return StreamingResponse(
        _stream_generator(request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        }
    )

async def _stream_generator(request: ChatRequest):
    # yields SSE-formatted strings throughout the agentic loop
    yield _sse({"type": "session_start", "session_id": request.session_id, "tenant_id": request.tenant_id})
    # ... agentic loop using asyncio.as_completed() for parallel tool calls ...
    # Cosmos DB write happens here before done
    yield _sse({"type": "done", "tokens_used": total_tokens, "session_id": request.session_id})

def _sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"
```

---

### 2. Gateway — `services/gateway`

#### New route

```
POST /chat/stream
```

Executes the same middleware chain as the existing `/chat` route (token validation, tenant extraction, rate limit, budget check), then proxies to the Coordinator's `/chat/stream` endpoint as a non-buffered streaming proxy.

```
Request in
  → validate ISE token → 401 if invalid
  → extract tenant_id
  → rate limit check → 429 if exceeded
  → token budget check (best-effort, current balance) → emit error SSE event + close if exceeded
  → open httpx streaming POST to Coordinator /chat/stream
  → return StreamingResponse wrapping the httpx async byte iterator
  → stream bytes to client as they arrive (never buffer)
  → on stream close: if done event was received, deduct tokens_used from tenant budget in Cosmos DB
  → on stream close without done event: log incomplete session to audit log, no budget deduction
```

#### Gateway StreamingResponse pattern

The Gateway returns a `StreamingResponse` that wraps an async generator iterating over the `httpx` response content. It does not buffer the full response:

```python
async def _proxy_stream(request: ChatRequest, tenant_id: str):
    async with httpx.AsyncClient() as client:
        async with client.stream("POST", COORDINATOR_URL + "/chat/stream", json=request.dict()) as response:
            tokens_used = None
            async for chunk in response.aiter_bytes():
                # Scan chunks for the done event to capture tokens_used
                # without interrupting the byte stream to the client
                if b'"type": "done"' in chunk:
                    try:
                        data = json.loads(chunk.split(b"data: ", 1)[1])
                        tokens_used = data.get("tokens_used")
                    except Exception:
                        pass
                yield chunk
            # Note: done event scanning operates on individual chunks. In rare cases
            # the done event may span a TCP chunk boundary and be missed. This is
            # safe — the Coordinator's Cosmos DB entry remains the authoritative
            # record; the Gateway simply logs an incomplete session. Implementations
            # may buffer a trailing two-chunk window to eliminate this edge case.
            # After stream closes, update budget
            if tokens_used is not None:
                await update_tenant_budget(tenant_id, tokens_used)
            else:
                await log_incomplete_session(tenant_id, request.session_id)

@app.post("/chat/stream")
async def chat_stream_proxy(request: ChatRequest, tenant_id: str = Depends(extract_tenant)):
    # ... auth, rate limit, budget checks ...
    return StreamingResponse(
        _proxy_stream(request, tenant_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        }
    )
```

**Budget accounting on disconnect:** The Coordinator writes the audit log entry to Cosmos DB before emitting `done` (step 10 above). If the client disconnects before `done` reaches the Gateway, the Coordinator's audit log entry still exists with the correct token count. The Gateway logs an incomplete session but does not need to independently record token consumption — the Coordinator's Cosmos DB entry is the authoritative record.

**Coordinator connection timeout:** If `httpx` cannot reach the Coordinator, the Gateway emits a single `error` SSE event before closing the stream:

```python
yield _sse({"type": "error", "code": "coordinator_unavailable", "message": "Service temporarily unavailable"})
```

**CORS:** The Gateway's existing CORS middleware covers the streaming endpoint. No additional CORS configuration is required. The `text/event-stream` response type does not introduce new preflight requirements beyond what the existing `POST /chat` route already handles.

---

### 3. SSE Event Schema

All events follow the SSE wire format:

```
data: {JSON object}\n\n
```

SSE `id:` and `retry:` fields are not used. Mid-stream reconnection and resumption are out of scope — if the stream drops, the UI surfaces a clean error rather than attempting to reconnect from a partial state.

#### Event types

| Event | Fields | When emitted |
|---|---|---|
| `session_start` | `session_id`, `tenant_id` | Before any agent calls |
| `agent_start` | `agent`, `detail: string \| null` | In immediate succession before parallel execution begins (`detail` is an agent-defined context string — device host for network_agent, null if not applicable) |
| `agent_complete` | `agent`, `duration_ms` | As each agent finishes (real-time, not batched) |
| `agent_error` | `agent`, `error` | When an agent fails (coordinator continues with partial results) |
| `token` | `content` | Per token of Claude's final streamed response |
| `done` | `tokens_used`, `session_id` | After Cosmos DB write, signals clean completion |
| `error` | `code`, `message` | Fatal errors (budget_exceeded, rate_limited, coordinator_unavailable) |

#### Example stream for a parallel audit

```
data: {"type": "session_start", "session_id": "s-abc123", "tenant_id": "tenant-a"}

data: {"type": "agent_start", "agent": "network_agent", "detail": "10.0.0.1"}

data: {"type": "agent_start", "agent": "enrichment_agent", "detail": null}

data: {"type": "agent_complete", "agent": "enrichment_agent", "duration_ms": 890}

data: {"type": "agent_complete", "agent": "network_agent", "duration_ms": 1240}

data: {"type": "agent_start", "agent": "rag_agent", "detail": null}

data: {"type": "agent_start", "agent": "itsm_agent", "detail": null}

data: {"type": "agent_complete", "agent": "rag_agent", "duration_ms": 620}

data: {"type": "agent_complete", "agent": "itsm_agent", "duration_ms": 1100}

data: {"type": "token", "content": "Based"}

data: {"type": "token", "content": " on"}

data: {"type": "token", "content": " the firewall"}

data: {"type": "done", "tokens_used": 1840, "session_id": "s-abc123"}
```

The two `agent_start` events for `network_agent` and `enrichment_agent` appear in immediate succession — they are emitted before either coroutine begins executing. Their `agent_complete` events then arrive as each finishes, in completion order (enrichment finished first at 890ms despite starting at the same time).

---

### 4. UI — `services/ui`

#### Layout

Two-column layout:

- **Main area (left/centre):** Conversation history. Each assistant message streams tokens in as they arrive. A subtle cursor indicates active generation.
- **Agent sidebar (right):** Persistent panel showing agent activity across the full session. Each message in the conversation has a collapsible entry listing which agents ran, timing, and any errors. Parallel agents appear as simultaneous rows that resolve independently.

#### SSE client

The UI uses `fetch` with a `ReadableStream` reader. The browser `EventSource` API is explicitly not used — it only supports `GET` requests and cannot send a request body.

The `useStream` hook manages the connection lifecycle:

```typescript
// services/ui/src/hooks/useStream.ts
async function* streamChat(request: ChatRequest): AsyncGenerator<SSEEvent> {
    const response = await fetch('/chat/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(request),
    });
    const reader = response.body!.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n\n');
        buffer = lines.pop()!;
        for (const line of lines) {
            if (line.startsWith('data: ')) {
                yield JSON.parse(line.slice(6)) as SSEEvent;
            }
        }
    }
}
```

On each event:

| Event | UI action |
|---|---|
| `session_start` | Initialise sidebar entry for this message |
| `agent_start` | Add agent row to sidebar (amber dot, "running") |
| `agent_complete` | Update agent row (green dot, duration) |
| `agent_error` | Update agent row (red dot, error message) |
| `token` | Append token to current assistant message bubble |
| `done` | Finalise message, close stream, update token counter |
| `error` | Show error state in message bubble, close stream |

#### Agent sidebar entry structure (per message)

```
▾ Message 3 — 4 agents
  ● network_agent      10.0.0.1     1.24s
  ● enrichment_agent               0.89s
  ● rag_agent                      0.62s
  ● itsm_agent                     1.10s
```

Parallel agent pairs are visually grouped by simultaneous start time. The sidebar retains all previous message entries collapsed by default.

---

## Multi-Tenancy

All existing multi-tenancy rules apply unchanged:

- `tenant_id` is carried on the `ChatRequest` and included in `session_start` and all Cosmos DB writes
- Conversation history loaded with partition key `tenant_id`
- Audit log entries include `tenant_id`, `session_id`, `tokens_used`, agents invoked
- Token budget check and deduction scoped to `tenant_id`
- `max_tokens` per Claude call loaded from tenant config document in Cosmos DB

---

## Error Handling

| Scenario | Behaviour |
|---|---|
| Specialist agent fails | Emit `agent_error`, coordinator continues with partial results — graceful degradation per existing architecture principle |
| All agents fail | Emit `agent_error` for each, Claude generates response from conversation context only |
| Budget exceeded (pre-flight) | Gateway emits `error` event with `code: budget_exceeded`, closes stream before Coordinator is called |
| Coordinator unreachable | Gateway emits `error` event with `code: coordinator_unavailable` |
| Claude API error | Coordinator emits `error` event, closes stream, logs to Cosmos DB |
| Client disconnect before `done` | Coordinator's Cosmos DB audit entry is the authoritative record; Gateway logs incomplete session |
| Cosmos DB write failure (step 10) | `done` is never emitted; client receives stream-level close without `done`; UI surfaces error |

---

## Files Changed

All service directories are new (greenfield). All files listed are to be created.

| File | Type | Purpose |
|---|---|---|
| `services/coordinator/main.py` | New | FastAPI app, `/chat/stream` and `/chat` endpoints |
| `services/coordinator/agent_loop.py` | New | Agentic loop with `asyncio.as_completed()` parallelism and SSE emission |
| `services/gateway/main.py` | New | FastAPI app, `/chat/stream` proxy route |
| `services/gateway/middleware/token_budget.py` | New | Budget check (pre-flight) and budget deduction (on `done`) |
| `services/ui/src/components/ChatWindow.tsx` | New | Token streaming into message bubbles |
| `services/ui/src/components/AgentSidebar.tsx` | New | Persistent agent activity panel |
| `services/ui/src/hooks/useStream.ts` | New | `fetch`-based SSE connection management hook |

---

## Out of Scope

- WebSocket support (SSE is sufficient)
- SSE `id:` / `retry:` fields and mid-stream reconnection (future enhancement)
- Mid-stream cancellation (future enhancement)
- Per-agent token attribution (tracked at session level only)
- Streaming for non-chat endpoints (health, admin)
