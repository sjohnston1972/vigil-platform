import asyncio
import json
import logging
import os
from datetime import datetime, timezone

from azure.cosmos.aio import CosmosClient
from azure.cosmos.exceptions import CosmosResourceNotFoundError
from azure.identity.aio import DefaultAzureCredential
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

_cosmos_client = None
conversations_container = None


@app.on_event("startup")
async def startup():
    global _cosmos_client, conversations_container
    credential = DefaultAzureCredential()
    _cosmos_client = CosmosClient(
        url=os.getenv("COSMOS_ENDPOINT"),
        credential=credential,
    )
    db = _cosmos_client.get_database_client(os.getenv("COSMOS_DATABASE"))
    conversations_container = db.get_container_client("conversations")
    from step_up import init_step_up_containers
    await init_step_up_containers(_cosmos_client, os.getenv("KEY_VAULT_URL", ""))
    asyncio.ensure_future(_run_recovery_loop())


async def _run_recovery_loop():
    from step_up import recover_expired_step_up_requests
    while True:
        try:
            await recover_expired_step_up_requests()
        except Exception as exc:
            logger.warning("Recovery loop error", exc_info=exc)
        await asyncio.sleep(300)


@app.get("/health")
def health():
    return {"status": "healthy", "service": "coordinator"}


# ── Pydantic models ────────────────────────────────────────────────────────────

class StepUpDecisionRequest(BaseModel):
    comment: str | None = None


class StepUpDecisionResponse(BaseModel):
    request_id: str
    status: str
    decided_by: str
    decided_at: str


class StepUpErrorResponse(BaseModel):
    error: str
    request_id: str
    detail: str | None = None


class SessionSummary(BaseModel):
    id: str
    tenant_id: str
    title: str
    agents: list[str]
    updated_at: str


class RenameTitleRequest(BaseModel):
    title: str


class AuthMeResponse(BaseModel):
    tenant_id: str


# ── Header extractors ──────────────────────────────────────────────────────────

def extract_tenant(x_tenant_id: str = Header(...)) -> str:
    return x_tenant_id


def extract_user(x_user_identity: str = Header(...)) -> str:
    return x_user_identity


# ── Internal helpers ───────────────────────────────────────────────────────────

async def _get_step_up_request(request_id: str, tenant_id: str) -> dict:
    db = _cosmos_client.get_database_client(os.getenv("COSMOS_DATABASE"))
    container = db.get_container_client("step_up_requests")
    return await container.read_item(item=request_id, partition_key=tenant_id)


async def _get_tenant_config(tenant_id: str) -> dict:
    db = _cosmos_client.get_database_client(os.getenv("COSMOS_DATABASE"))
    container = db.get_container_client("tenant_config")
    return await container.read_item(item=tenant_id, partition_key=tenant_id)


async def _write_step_up_decision(
    record: dict,
    status: str,
    decided_by: str,
    tenant_id: str,
) -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    record["status"] = status
    record["approved_by"] = decided_by   # set for both approve and reject
    record["approved_at"] = now_iso
    db = _cosmos_client.get_database_client(os.getenv("COSMOS_DATABASE"))
    container = db.get_container_client("step_up_requests")
    await container.replace_item(item=record["id"], body=record, partition_key=tenant_id)
    logger.info(
        "Step-up decision recorded",
        extra={
            "tenant_id": tenant_id,
            "request_id": record["id"],
            "tool_name": record["tool_name"],
            "status": status,
            "requested_by": record["requested_by"],
            "approved_by": decided_by,
        },
    )


async def _propagate_approval_to_change_records(record: dict, decided_by: str) -> None:
    """
    For apply_change and rollback_change: write approved_by to the change_records document.
    change_id lives in record["context"]["change_id"].
    """
    tool_name = record["tool_name"]
    if tool_name not in ("apply_change", "rollback_change"):
        return
    change_id = record.get("context", {}).get("change_id")
    if not change_id:
        return
    tenant_id = record["tenant_id"]
    db = _cosmos_client.get_database_client(os.getenv("COSMOS_DATABASE"))
    change_container = db.get_container_client("change_records")
    change_record = await change_container.read_item(item=change_id, partition_key=tenant_id)
    change_record["approved_by"] = decided_by
    change_record["approved_at"] = record["approved_at"]
    await change_container.replace_item(item=change_id, body=change_record, partition_key=tenant_id)


def _check_authorisation(record: dict, tenant_config: dict, caller: str, action: str) -> None:
    """
    Raises HTTPException if the caller is not authorised to approve or reject.
    Rules apply symmetrically to both approve and reject.
    Logs all forbidden attempts to the audit log (spec requirement).
    """
    policy = tenant_config.get("step_up_policy", {}).get(record["tool_name"], {})
    approvers = tenant_config.get("step_up_approvers", [])
    self_approve = policy.get("self_approve", False)

    def _log_and_raise(error_code: str) -> None:
        logger.warning(
            "Step-up authorisation denied",
            extra={
                "tenant_id": record["tenant_id"],
                "request_id": record["id"],
                "tool_name": record["tool_name"],
                "caller": caller,
                "action": action,
                "reason": error_code,
            },
        )
        raise HTTPException(
            status_code=403,
            detail=StepUpErrorResponse(
                error=error_code,
                request_id=record["id"],
            ).model_dump(),
        )

    if self_approve:
        if caller != record["requested_by"] and caller not in approvers:
            _log_and_raise("not_authorised_approver")
    else:
        if caller not in approvers:
            _log_and_raise("not_authorised_approver")
        if caller == record["requested_by"]:
            _log_and_raise("self_approval_not_permitted")


# ── Endpoints ──────────────────────────────────────────────────────────────────

@app.get("/auth/me", response_model=AuthMeResponse)
async def auth_me(tenant_id: str = Depends(extract_tenant)):
    """Return the authenticated tenant identity from the JWT header."""
    return AuthMeResponse(tenant_id=tenant_id)


@app.get("/sessions", response_model=list[SessionSummary])
async def list_sessions(tenant_id: str = Depends(extract_tenant)):
    """List all conversation sessions for the tenant, newest first."""
    results = []
    async for item in conversations_container.query_items(
        query="SELECT c.id, c.tenant_id, c.title, c.agents, c.updated_at FROM c WHERE c.tenant_id = @tid ORDER BY c.updated_at DESC",
        parameters=[{"name": "@tid", "value": tenant_id}],
        partition_key=tenant_id,
    ):
        results.append(SessionSummary(**item))
    return results


@app.patch("/sessions/{session_id}/title", response_model=SessionSummary)
async def rename_session(
    session_id: str,
    body: RenameTitleRequest,
    tenant_id: str = Depends(extract_tenant),
):
    """Rename a session. Returns 404 if not found, 403 if wrong tenant."""
    try:
        record = await conversations_container.read_item(item=session_id, partition_key=tenant_id)
    except CosmosResourceNotFoundError:
        raise HTTPException(status_code=404, detail="session not found")
    record["title"] = body.title.strip()
    record["updated_at"] = datetime.now(timezone.utc).isoformat()
    await conversations_container.replace_item(item=session_id, body=record, partition_key=tenant_id)
    return SessionSummary(**record)


@app.post("/step-up/{request_id}/approve", response_model=StepUpDecisionResponse)
async def step_up_approve(
    request_id: str,
    body: StepUpDecisionRequest,
    tenant_id: str = Depends(extract_tenant),
    caller: str = Depends(extract_user),
):
    try:
        record = await _get_step_up_request(request_id, tenant_id)
    except Exception:
        raise HTTPException(
            status_code=404,
            detail=StepUpErrorResponse(error="request_not_found", request_id=request_id).model_dump(),
        )

    if record["status"] != "pending":
        raise HTTPException(
            status_code=409,
            detail=StepUpErrorResponse(error="already_decided", request_id=request_id).model_dump(),
        )

    expires_at = datetime.fromisoformat(record["expires_at"])
    if datetime.now(timezone.utc) >= expires_at:
        raise HTTPException(
            status_code=409,
            detail=StepUpErrorResponse(error="request_expired", request_id=request_id).model_dump(),
        )

    tenant_config = await _get_tenant_config(tenant_id)
    _check_authorisation(record, tenant_config, caller, "approve")

    decided_at = datetime.now(timezone.utc).isoformat()
    await _write_step_up_decision(record, "approved", caller, tenant_id)
    await _propagate_approval_to_change_records(record, caller)

    return StepUpDecisionResponse(
        request_id=request_id,
        status="approved",
        decided_by=caller,
        decided_at=decided_at,
    )


@app.post("/step-up/{request_id}/reject", response_model=StepUpDecisionResponse)
async def step_up_reject(
    request_id: str,
    body: StepUpDecisionRequest,
    tenant_id: str = Depends(extract_tenant),
    caller: str = Depends(extract_user),
):
    try:
        record = await _get_step_up_request(request_id, tenant_id)
    except Exception:
        raise HTTPException(
            status_code=404,
            detail=StepUpErrorResponse(error="request_not_found", request_id=request_id).model_dump(),
        )

    if record["status"] != "pending":
        raise HTTPException(
            status_code=409,
            detail=StepUpErrorResponse(error="already_decided", request_id=request_id).model_dump(),
        )

    expires_at = datetime.fromisoformat(record["expires_at"])
    if datetime.now(timezone.utc) >= expires_at:
        raise HTTPException(
            status_code=409,
            detail=StepUpErrorResponse(error="request_expired", request_id=request_id).model_dump(),
        )

    tenant_config = await _get_tenant_config(tenant_id)
    _check_authorisation(record, tenant_config, caller, "reject")

    decided_at = datetime.now(timezone.utc).isoformat()
    await _write_step_up_decision(record, "rejected", caller, tenant_id)

    return StepUpDecisionResponse(
        request_id=request_id,
        status="rejected",
        decided_by=caller,
        decided_at=decided_at,
    )
