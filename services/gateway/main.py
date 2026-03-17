import logging
import os

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
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
