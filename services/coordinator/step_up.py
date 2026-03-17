"""
Step-up auth lifecycle — human-in-the-loop approval gate for high-risk coordinator tools.

IMPORTANT: write credentials fetched here are a deliberate exception to the startup-fetch rule.
They are fetched just-in-time after approval (not cached) so revocation takes immediate effect.
See CLAUDE.md for the documented exception.
"""

import asyncio
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from azure.cosmos.aio import CosmosClient
from azure.identity.aio import DefaultAzureCredential
from azure.keyvault.secrets.aio import SecretClient

logger = logging.getLogger(__name__)

# Module-level containers — initialised at startup via init_step_up_containers()
_step_up_container = None
_grants_container = None
_tenant_config_container = None
_kv_client = None


async def init_step_up_containers(cosmos_client: CosmosClient, kv_url: str):
    """Call once at coordinator startup to wire up Cosmos DB containers and Key Vault client."""
    global _step_up_container, _grants_container, _tenant_config_container, _kv_client

    db = cosmos_client.get_database_client(os.getenv("COSMOS_DATABASE"))
    _step_up_container = db.get_container_client("step_up_requests")
    _grants_container = db.get_container_client("step_up_grants")
    _tenant_config_container = db.get_container_client("tenant_config")

    credential = DefaultAzureCredential()
    _kv_client = SecretClient(vault_url=kv_url, credential=credential)


@dataclass
class StepUpResult:
    status: str           # "approved" | "rejected" | "expired" | "failed"
    request_id: str
    approved_by: Optional[str]
    credential: Optional[str]   # populated only on "approved"


async def create_step_up_request(
    tool_name: str,
    tool_input: dict,
    request,           # ChatRequest — avoid circular import with type annotation
    policy: dict,
) -> dict:
    """
    Creates a step_up_requests record in Cosmos DB and returns the document.
    The caller should emit approval_required SSE immediately after this returns,
    then call resolve_step_up to block until a decision is made.
    """
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=policy["pending_ttl_seconds"])
    request_id = f"sur-{uuid.uuid4()}"

    context = _build_context(tool_name, tool_input)

    record = {
        "id": request_id,
        "tenant_id": request.tenant_id,
        "session_id": request.session_id,
        "tool_name": tool_name,
        "requested_by": request.user_identity,
        "requested_at": now.isoformat(),
        "status": "pending",
        "context": context,
        "approved_by": None,
        "approved_at": None,
        "expires_at": expires_at.isoformat(),
        "grant_type": policy["grant_type"],
        "grant_duration_seconds": policy.get("grant_duration_seconds"),
    }

    await _step_up_container.create_item(body=record, enable_automatic_id_generation=False)

    logger.info(
        "Step-up request created",
        extra={
            "tenant_id": request.tenant_id,
            "request_id": request_id,
            "tool_name": tool_name,
            "expires_at": expires_at.isoformat(),
        },
    )
    return record


def _build_context(tool_name: str, tool_input: dict) -> dict:
    """Extract a safe, human-readable summary from tool input for the approval prompt."""
    context = {"tool": tool_name}
    safe_keys = {"change_id", "device_host", "summary", "ticket_id", "action"}
    for key in safe_keys:
        if key in tool_input:
            context[key] = tool_input[key]
    if "summary" not in context:
        context["summary"] = f"Tool: {tool_name}"
    return context


async def get_active_grant(tenant_id: str, session_id: str, tool_name: str) -> Optional[dict]:
    """
    Returns an active time-window grant for this session+tool, or None.
    Uses partition_key=tenant_id — never a cross-partition query.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    results = list(
        _grants_container.query_items(
            query="""SELECT * FROM c
                     WHERE c.session_id = @session_id
                       AND c.tool_name  = @tool_name
                       AND c.expires_at  > @now""",
            parameters=[
                {"name": "@session_id", "value": session_id},
                {"name": "@tool_name",  "value": tool_name},
                {"name": "@now",        "value": now_iso},
            ],
            partition_key=tenant_id,
        )
    )
    return results[0] if results else None


async def write_active_grant(
    tool_name: str,
    approved_by: str,
    request,
    policy: dict,
) -> None:
    """Write a time-window grant to step_up_grants after approval. Cosmos DB TTL handles expiry."""
    now = datetime.now(timezone.utc)
    duration = policy["grant_duration_seconds"]
    expires_at = now + timedelta(seconds=duration)
    grant = {
        "id": f"grnt-{uuid.uuid4()}",
        "tenant_id": request.tenant_id,
        "session_id": request.session_id,
        "tool_name": tool_name,
        "approved_by": approved_by,
        "granted_at": now.isoformat(),
        "expires_at": expires_at.isoformat(),
        "_ttl": duration,  # Cosmos DB per-document TTL (requires container default_ttl=-1)
    }
    await _grants_container.create_item(body=grant, enable_automatic_id_generation=False)
    logger.info(
        "Active grant written",
        extra={"tenant_id": request.tenant_id, "tool_name": tool_name, "expires_at": expires_at.isoformat()},
    )


async def mark_step_up_failed(request_id: str, tenant_id: str) -> None:
    """Mark a step-up request as failed (approved but Key Vault credential fetch failed)."""
    record = await _step_up_container.read_item(item=request_id, partition_key=tenant_id)
    record["status"] = "failed"
    await _step_up_container.replace_item(item=record["id"], body=record, partition_key=tenant_id)
    logger.warning(
        "Step-up request marked failed",
        extra={"tenant_id": tenant_id, "request_id": request_id},
    )


async def await_step_up_decision(request_id: str, tenant_id: str, policy: dict) -> dict:
    """
    Polls step_up_requests until status is no longer 'pending', or expires_at is reached.
    Uses point reads (read_item) with (request_id, tenant_id) — never a cross-partition query.
    Capped at pending_ttl_seconds. Caller is responsible for SSE keepalive heartbeats.
    """
    backoff_seconds = [1, 2, 4, 8, 10]
    backoff_idx = 0

    while True:
        record = await _step_up_container.read_item(item=request_id, partition_key=tenant_id)

        if record["status"] != "pending":
            return record

        expires_at = datetime.fromisoformat(record["expires_at"])
        if datetime.now(timezone.utc) >= expires_at:
            record["status"] = "expired"
            await _step_up_container.replace_item(item=record["id"], body=record, partition_key=tenant_id)
            logger.info("Step-up request expired", extra={"tenant_id": tenant_id, "request_id": request_id})
            return record

        sleep_for = backoff_seconds[min(backoff_idx, len(backoff_seconds) - 1)]
        backoff_idx += 1
        await asyncio.sleep(sleep_for)


async def fetch_tool_credential(tool_name: str, tenant_id: str) -> str:
    """
    Fetches the write credential for a tool from Azure Key Vault just-in-time.

    DELIBERATE EXCEPTION to the startup-fetch rule: these credentials are only fetched
    after human approval and are never cached, so revocation takes immediate effect.
    Secret name format: tenant-{tenant_id}-{tool-name}-write-credential
    (underscores in tool_name replaced with hyphens — Key Vault names allow only hyphens)
    """
    tool_name_hyphenated = tool_name.replace("_", "-")
    secret_name = f"tenant-{tenant_id}-{tool_name_hyphenated}-write-credential"
    secret = await _kv_client.get_secret(secret_name)
    return secret.value


async def prepare_step_up(
    tool_name: str,
    tool_input: dict,
    request,
    policy: dict,
    tenant_config: dict,
) -> Optional[dict]:
    """
    Creates the step_up_requests record and fires out-of-band notifications.
    Returns the request document so the caller can emit approval_required SSE immediately.
    Returns None if an active time-window grant exists — caller proceeds without approval.
    """
    if policy["grant_type"] == "time_window":
        grant = await get_active_grant(request.tenant_id, request.session_id, tool_name)
        if grant:
            logger.info(
                "Active time-window grant found — skipping step-up",
                extra={"tenant_id": request.tenant_id, "tool_name": tool_name},
            )
            return None

    step_up_req = await create_step_up_request(tool_name, tool_input, request, policy)

    channels = policy.get("notification_channels", [])
    if "email" in channels or "webhook" in channels:
        asyncio.ensure_future(_notify_out_of_band(step_up_req, tenant_config))

    return step_up_req


async def resolve_step_up(
    step_up_req: dict,
    request,
    policy: dict,
) -> StepUpResult:
    """
    Polls for the approval decision, then fetches the Key Vault credential on approval.
    Writes an active grant for time_window tools.
    Call this after emitting approval_required SSE — it blocks until a decision is made.
    """
    decision = await await_step_up_decision(step_up_req["id"], request.tenant_id, policy)

    if decision["status"] in ("rejected", "expired"):
        return StepUpResult(
            status=decision["status"],
            request_id=step_up_req["id"],
            approved_by=decision.get("approved_by"),
            credential=None,
        )

    # Approved — fetch credential just-in-time (deliberate KV exception, see module docstring)
    try:
        credential = await fetch_tool_credential(step_up_req["tool_name"], request.tenant_id)
    except Exception as exc:
        logger.error(
            "Key Vault credential fetch failed after approval",
            extra={"tenant_id": request.tenant_id, "request_id": step_up_req["id"]},
            exc_info=exc,
        )
        await mark_step_up_failed(step_up_req["id"], request.tenant_id)
        return StepUpResult(
            status="failed",
            request_id=step_up_req["id"],
            approved_by=decision["approved_by"],
            credential=None,
        )

    if policy["grant_type"] == "time_window":
        await write_active_grant(step_up_req["tool_name"], decision["approved_by"], request, policy)

    return StepUpResult(
        status="approved",
        request_id=step_up_req["id"],
        approved_by=decision["approved_by"],
        credential=credential,
    )


async def recover_expired_step_up_requests() -> None:
    """
    Expires step_up_requests records that are still pending past their expires_at.
    Runs at startup and on a 5-minute schedule.

    INTENTIONAL cross-partition read: tenant_config_container.read_all_items() is the
    only permitted cross-partition query in this service — used solely to bootstrap
    the tenant ID list. All subsequent queries use partition_key=tenant_id.
    """
    now_iso = datetime.now(timezone.utc).isoformat()

    tenant_ids = [doc["tenant_id"] for doc in _tenant_config_container.read_all_items()]

    for tenant_id in tenant_ids:
        stuck = _step_up_container.query_items(
            query="SELECT * FROM c WHERE c.status = 'pending' AND c.expires_at < @now",
            parameters=[{"name": "@now", "value": now_iso}],
            partition_key=tenant_id,
        )
        for record in stuck:
            record["status"] = "expired"
            await _step_up_container.replace_item(
                item=record["id"], body=record, partition_key=tenant_id
            )
            logger.info(
                "Recovery: expired stale step-up request",
                extra={"tenant_id": tenant_id, "request_id": record["id"]},
            )


async def _notify_out_of_band(step_up_req: dict, tenant_config: dict) -> None:
    """Internal helper — delegates to notifications module. Never raises."""
    try:
        from notifications import notify_approvers
        await notify_approvers(step_up_req, tenant_config)
    except Exception as exc:
        logger.warning(
            "Out-of-band notification failed — in-chat path unaffected",
            extra={"tenant_id": step_up_req["tenant_id"], "request_id": step_up_req["id"]},
            exc_info=exc,
        )
