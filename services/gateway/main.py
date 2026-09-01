import json
import logging
import os
from typing import AsyncIterator

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

COORDINATOR_URL = os.getenv("COORDINATOR_URL", "http://coordinator:8000")

_cosmos_client = None
tenant_config_container = None


@app.on_event("startup")
async def startup():
    """
    Set up the Cosmos DB client used for per-tenant token budget enforcement.
    Uses Azure Managed Identity — no connection strings or keys in env vars.
    """
    global _cosmos_client, tenant_config_container
    from azure.cosmos.aio import CosmosClient
    from azure.identity.aio import DefaultAzureCredential

    credential = DefaultAzureCredential()
    _cosmos_client = CosmosClient(
        url=os.getenv("COSMOS_ENDPOINT"),
        credential=credential,
    )
    db = _cosmos_client.get_database_client(os.getenv("COSMOS_DATABASE"))
    tenant_config_container = db.get_container_client("tenant_config")


@app.get("/health")
def health():
    return {"status": "healthy", "service": "gateway"}


def validate_ise_token(request: Request) -> dict:
    """
    Validates the ISE-issued token from the Authorization header and extracts claims.

    Returns dict with tenant_id and user_identity.
    Raises HTTPException 401 if the token is missing or invalid.

    Delegates to middleware.auth.validate_token for signature verification.
    """
    from middleware.auth import validate_token

    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    token = auth_header.split(" ", 1)[1]
    if not token:
        raise HTTPException(status_code=401, detail="Empty token")
    return validate_token(token)


async def _proxy_json(method: str, path: str, claims: dict, body: dict) -> httpx.Response:
    """
    Forwards a JSON request to the Coordinator, passing tenant_id and user identity
    as headers (set by Gateway after token validation — never trusted from client).
    """
    headers = {
        "X-Tenant-Id": claims["tenant_id"],
        "X-User-Identity": claims["user_identity"],
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await getattr(client, method)(
            f"{COORDINATOR_URL}{path}",
            json=body,
            headers=headers,
        )
    return resp


async def _proxy_get(path: str, claims: dict) -> httpx.Response:
    """
    Forwards a GET request to the Coordinator, passing tenant_id and user identity
    as headers (set by Gateway after token validation — never trusted from client).
    """
    headers = {
        "X-Tenant-Id": claims["tenant_id"],
        "X-User-Identity": claims["user_identity"],
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.get(f"{COORDINATOR_URL}{path}", headers=headers)
    return resp


class StepUpDecisionRequest(BaseModel):
    comment: str | None = None


@app.post("/step-up/{request_id}/approve")
async def step_up_approve_proxy(
    request_id: str,
    body: StepUpDecisionRequest,
    request: Request,
):
    """
    Proxy for POST /step-up/{request_id}/approve.
    Auth middleware validates the ISE token; authorisation logic lives in the Coordinator.
    """
    claims = validate_ise_token(request)
    resp = await _proxy_json("post", f"/step-up/{request_id}/approve", claims, body.model_dump())
    return JSONResponse(status_code=resp.status_code, content=resp.json())


@app.post("/step-up/{request_id}/reject")
async def step_up_reject_proxy(
    request_id: str,
    body: StepUpDecisionRequest,
    request: Request,
):
    """
    Proxy for POST /step-up/{request_id}/reject.
    Same auth chain as approve.
    """
    claims = validate_ise_token(request)
    resp = await _proxy_json("post", f"/step-up/{request_id}/reject", claims, body.model_dump())
    return JSONResponse(status_code=resp.status_code, content=resp.json())


# ── Auth / sessions proxies ──────────────────────────────────────────────────────

class RenameTitleRequest(BaseModel):
    title: str


@app.get("/auth/me")
async def auth_me_proxy(request: Request):
    """Proxy for GET /auth/me — returns the authenticated tenant identity."""
    claims = validate_ise_token(request)
    resp = await _proxy_get("/auth/me", claims)
    return JSONResponse(status_code=resp.status_code, content=resp.json())


@app.get("/sessions")
async def sessions_list_proxy(request: Request):
    """Proxy for GET /sessions — lists conversation sessions for the tenant."""
    claims = validate_ise_token(request)
    resp = await _proxy_get("/sessions", claims)
    return JSONResponse(status_code=resp.status_code, content=resp.json())


@app.patch("/sessions/{session_id}/title")
async def sessions_rename_proxy(session_id: str, body: RenameTitleRequest, request: Request):
    """Proxy for PATCH /sessions/{session_id}/title — renames a session."""
    claims = validate_ise_token(request)
    resp = await _proxy_json("patch", f"/sessions/{session_id}/title", claims, body.model_dump())
    return JSONResponse(status_code=resp.status_code, content=resp.json())


# ── Chat SSE proxy ──────────────────────────────────────────────────────────────

SSE_STREAM_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


class ChatRequest(BaseModel):
    session_id: str
    tenant_id: str | None = None
    messages: list[dict]


def _sse_bytes(data: dict) -> bytes:
    return f"data: {json.dumps(data)}\n\n".encode("utf-8")


async def _stream_from_coordinator(headers: dict, body: dict) -> AsyncIterator[bytes]:
    """
    Open a non-buffered streaming POST to the Coordinator's /chat/stream and yield
    raw SSE bytes to the caller as they arrive — never accumulated in memory.

    Isolated from the route handler so tests can substitute a stub upstream (with
    or without artificial delays between chunks) without any real network call.
    """
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=None, write=10.0, pool=10.0)) as client:
            async with client.stream("POST", f"{COORDINATOR_URL}/chat/stream", json=body, headers=headers) as response:
                async for chunk in response.aiter_bytes():
                    if chunk:
                        yield chunk
    except httpx.HTTPError as exc:
        logger.error("Coordinator unavailable for /chat/stream: %s", exc)
        yield _sse_bytes({
            "type": "error",
            "code": "coordinator_unavailable",
            "message": "Service temporarily unavailable",
        })


_DONE_MARKER = b'"type": "done"'


def _extract_tokens_used(chunk: bytes) -> int | None:
    """
    Best-effort extraction of tokens_used from a `done` SSE frame contained in a
    single proxied chunk. Scanning operates per-chunk without interrupting the
    byte stream to the client. In rare cases the `done` event may span a chunk
    boundary and be missed — that is safe: the Coordinator's own Cosmos DB
    conversation + audit record (written before it emits `done`) remains the
    authoritative record of tokens consumed either way; a miss here only means
    the Gateway's running budget total lags until the next successful request.
    """
    for line in chunk.split(b"\n"):
        line = line.strip()
        if line.startswith(b"data: ") and _DONE_MARKER in line:
            try:
                data = json.loads(line[len(b"data: "):])
            except Exception:
                return None
            return data.get("tokens_used")
    return None


async def _deduct_tenant_tokens(tenant_id: str, tokens_used: int) -> None:
    from middleware.token_budget import deduct_tokens

    await deduct_tokens(tenant_id, tokens_used, tenant_config_container)


async def _proxy_chat_stream(headers: dict, body: dict, tenant_id: str, session_id: str) -> AsyncIterator[bytes]:
    """
    Wraps _stream_from_coordinator: relays every chunk to the client immediately
    (never buffers), while scanning for the `done` event to capture tokens_used
    for budget deduction after the stream closes. If the stream ends without a
    `done` event (client disconnect, upstream failure), no deduction is
    attempted — the Coordinator's Cosmos DB entry remains authoritative.
    """
    tokens_used = None
    async for chunk in _stream_from_coordinator(headers, body):
        extracted = _extract_tokens_used(chunk)
        if extracted is not None:
            tokens_used = extracted
        yield chunk

    if tokens_used is not None:
        await _deduct_tenant_tokens(tenant_id, tokens_used)
    else:
        logger.info(
            "chat/stream ended without a done event — no budget deduction",
            extra={"tenant_id": tenant_id, "session_id": session_id},
        )


async def _single_error_stream(code: str, message: str) -> AsyncIterator[bytes]:
    yield _sse_bytes({"type": "error", "code": code, "message": message})


def _sse_error_response(code: str, message: str) -> StreamingResponse:
    """
    An SSE-compatible error response: HTTP 200 with a text/event-stream body
    containing a single `error` event, then the stream closes. The UI's fetch
    based SSE client only parses the streamed body when the response status is
    ok — a bare 4xx/5xx status would be shown as a generic "Gateway error"
    instead of the structured error event, so pre-flight rejections (rate
    limit, budget) are delivered this way rather than as a plain HTTP error.
    """
    return StreamingResponse(
        _single_error_stream(code, message),
        media_type="text/event-stream",
        headers=SSE_STREAM_HEADERS,
    )


async def _check_tenant_budget(tenant_id: str):
    from middleware.token_budget import check_budget

    return await check_budget(tenant_id, tenant_config_container)


@app.post("/chat/stream")
async def chat_stream_proxy(body: ChatRequest, request: Request):
    """
    Non-buffering SSE reverse-proxy for POST /chat/stream.

    Validates the ISE token, enforces the tenant's per-minute rate limit and
    token budget *before* the Coordinator is ever called, then streams the
    Coordinator's response back to the client chunk-by-chunk as it arrives (no
    buffering of the full response). The client-supplied tenant_id in the
    request body is never trusted — the identity headers (and the tenant_id
    forwarded to the Coordinator) always come from the server-validated token
    claims.
    """
    from middleware.rate_limit import check_rate_limit

    claims = validate_ise_token(request)
    tenant_id = claims["tenant_id"]

    if not check_rate_limit(tenant_id):
        logger.info("Rate limit exceeded", extra={"tenant_id": tenant_id})
        return _sse_error_response("rate_limited", "Rate limit exceeded — please try again shortly.")

    budget_status = await _check_tenant_budget(tenant_id)
    if not budget_status.allowed:
        logger.info(
            "Token budget check rejected request",
            extra={"tenant_id": tenant_id, "reason": budget_status.reason},
        )
        return _sse_error_response("budget_exceeded", "Token budget exceeded for this tenant.")

    headers = {
        "X-Tenant-Id": tenant_id,
        "X-User-Identity": claims["user_identity"],
        "Content-Type": "application/json",
    }
    forward_body = body.model_dump()
    forward_body["tenant_id"] = tenant_id

    return StreamingResponse(
        _proxy_chat_stream(headers, forward_body, tenant_id, body.session_id),
        media_type="text/event-stream",
        headers=SSE_STREAM_HEADERS,
    )
