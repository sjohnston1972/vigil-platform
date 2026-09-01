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


@app.post("/chat/stream")
async def chat_stream_proxy(body: ChatRequest, request: Request):
    """
    Non-buffering SSE reverse-proxy for POST /chat/stream.

    Validates the ISE token, then streams the Coordinator's response back to the
    client chunk-by-chunk as it arrives (no buffering of the full response).
    The client-supplied tenant_id in the request body is never trusted — the
    identity headers (and the tenant_id forwarded to the Coordinator) always come
    from the server-validated token claims.
    """
    claims = validate_ise_token(request)

    headers = {
        "X-Tenant-Id": claims["tenant_id"],
        "X-User-Identity": claims["user_identity"],
        "Content-Type": "application/json",
    }
    forward_body = body.model_dump()
    forward_body["tenant_id"] = claims["tenant_id"]

    return StreamingResponse(
        _stream_from_coordinator(headers, forward_body),
        media_type="text/event-stream",
        headers=SSE_STREAM_HEADERS,
    )
