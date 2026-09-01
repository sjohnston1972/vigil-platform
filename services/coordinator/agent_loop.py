"""
Agentic loop for the VIGIL coordinator.

`_stream_generator` is the entry point driving one POST /chat/stream request:
session_start -> Claude tool-use loop -> token* -> done.

SSE event emission follows this order per tool call:
  1. If step-up required: approval_required (immediate, before poll)
  2. If step-up: approval_granted | approval_rejected | approval_expired
  3. agent_start
  4. agent_complete | agent_error

Step-up gated tools run serially. Non-gated tools run in parallel via asyncio.as_completed().
"""

import asyncio
import json
import logging
import os
import time
from typing import AsyncGenerator, Optional

import httpx

from model_client import FoundryModelClient, ModelClient
from step_up import (
    StepUpResult,
    fetch_tool_credential,
    prepare_step_up,
    resolve_step_up,
)

logger = logging.getLogger(__name__)

# SSE keepalive interval (seconds) — sent during long approval polls to prevent ACA idle timeout
_KEEPALIVE_INTERVAL = 30

# Default max_tokens for a chat turn when tenant_config doesn't specify one.
_DEFAULT_MAX_TOKENS = 4096

_SYSTEM_PROMPT = (
    "You are the VIGIL coordinator agent for a managed network and security services "
    "platform. You orchestrate specialist agents (network, knowledge-base/RAG, ITSM, "
    "vulnerability enrichment) to answer operator questions and to carry out approved "
    "network changes. Use a tool whenever it would produce a more accurate or current "
    "answer than your own knowledge — never fabricate device state, ticket IDs, or "
    "vulnerability data. Explain findings clearly and concisely for a network/security "
    "engineer audience."
)

# Lazily-constructed singleton — real Azure AI Foundry client, never built in tests.
_model_client_singleton: Optional[ModelClient] = None


def _get_model_client() -> ModelClient:
    """
    Returns the process-wide ModelClient, constructing it on first use.

    Tests never call this directly — they patch `agent_loop._get_model_client`
    to return a fake ModelClient so no real Azure AI Foundry call is ever made
    from the test suite.
    """
    global _model_client_singleton
    if _model_client_singleton is None:
        _model_client_singleton = FoundryModelClient(
            endpoint=os.environ["AZURE_FOUNDRY_ENDPOINT"],
            model=os.environ["AZURE_FOUNDRY_MODEL"],
        )
    return _model_client_singleton


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


def _keepalive() -> str:
    """SSE comment line — invisible to event parsers, prevents idle connection timeout."""
    return ": keepalive\n\n"


async def _call_agent(tool_name: str, tool_input: dict, request, credential: str | None = None) -> dict:
    """
    Dispatch a tool call to the appropriate specialist agent.
    credential is passed only for step-up gated write operations.
    """
    agent_url = _get_agent_url(tool_name)
    payload = {**tool_input, "tenant_id": request.tenant_id, "session_id": request.session_id}
    if credential:
        payload["write_credential"] = credential
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(agent_url, json=payload)
        resp.raise_for_status()
        return resp.json()


def _get_agent_url(tool_name: str) -> str:
    """Map tool name to agent internal URL."""
    import os
    urls = {
        "network_agent":    os.getenv("NETWORK_AGENT_URL", ""),
        "rag_agent":        os.getenv("RAG_AGENT_URL", ""),
        "itsm_agent":       os.getenv("ITSM_AGENT_URL", ""),
        "enrichment_agent": os.getenv("ENRICHMENT_AGENT_URL", ""),
        "apply_change":     os.getenv("NETWORK_AGENT_URL", ""),
        "rollback_change":  os.getenv("NETWORK_AGENT_URL", ""),
    }
    return urls.get(tool_name, "")


async def _stream_tool_call(
    tool_name: str,
    tool_input: dict,
    request,
    tenant_config: dict,
) -> AsyncGenerator[str, None]:
    """
    Yields SSE strings for a single tool call, handling the step-up gate if required.
    Designed to be called directly from _stream_generator for step-up tools.
    Non-gated tools should use asyncio.as_completed() in the outer loop instead.
    """
    policy = tenant_config.get("step_up_policy", {}).get(tool_name)

    if policy is None:
        # No gate — dispatch immediately (caller handles parallelism)
        await _call_agent(tool_name, tool_input, request)
        return

    # Step-up path
    step_up_req = await prepare_step_up(tool_name, tool_input, request, policy, tenant_config)

    if step_up_req is not None:
        # Yield approval_required IMMEDIATELY before blocking on the poll
        yield _sse({
            "type": "approval_required",
            "request_id": step_up_req["id"],
            "tool": tool_name,
            "context": step_up_req["context"],
            "approver_type": "self" if policy.get("self_approve") else "designated",
            "expires_at": step_up_req["expires_at"],
        })

        # Poll with keepalive heartbeats every _KEEPALIVE_INTERVAL seconds to prevent
        # ACA idle connection timeout during long approval waits.
        # asyncio.shield() keeps resolve_step_up running even when wait_for raises TimeoutError.
        resolve_task = asyncio.create_task(resolve_step_up(step_up_req, request, policy))
        while not resolve_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(resolve_task), timeout=_KEEPALIVE_INTERVAL)
            except asyncio.TimeoutError:
                yield _keepalive()
        step_up_result = resolve_task.result()

        if step_up_result.status == "rejected":
            yield _sse({
                "type": "approval_rejected",
                "request_id": step_up_result.request_id,
                "tool": tool_name,
                "decided_by": step_up_result.approved_by,
            })
            return

        if step_up_result.status == "expired":
            yield _sse({
                "type": "approval_expired",
                "request_id": step_up_result.request_id,
                "tool": tool_name,
            })
            return

        if step_up_result.status == "failed":
            yield _sse({
                "type": "agent_error",
                "agent": tool_name,
                "error": "credential_fetch_failed",
            })
            return

        yield _sse({
            "type": "approval_granted",
            "request_id": step_up_result.request_id,
            "tool": tool_name,
            "approved_by": step_up_result.approved_by,
        })
        credential = step_up_result.credential

    else:
        # Active time-window grant — fetch credential just-in-time, no approval events
        credential = await fetch_tool_credential(tool_name, request.tenant_id)

    # Dispatch the tool call with the credential
    yield _sse({"type": "agent_start", "agent": tool_name, "detail": tool_input.get("device_host")})
    try:
        start = time.monotonic()
        await _call_agent(tool_name, tool_input, request, credential=credential)
        duration_ms = int((time.monotonic() - start) * 1000)
        yield _sse({"type": "agent_complete", "agent": tool_name, "duration_ms": duration_ms})
    except Exception as exc:
        logger.error("Agent call failed", extra={"tool": tool_name}, exc_info=exc)
        yield _sse({"type": "agent_error", "agent": tool_name, "error": str(exc)})


async def _stream_generator(request, tenant_config: dict) -> AsyncGenerator[str, None]:
    """
    Drives one POST /chat/stream request end to end.

    Text-only skeleton (#13): emits session_start, calls the model client for a single
    turn with no tools registered, forwards text deltas as `token` events, then emits
    `done`. Tool dispatch and Cosmos DB persistence are layered on by sibling
    sub-issues (#14, #15) without changing this event contract.
    """
    yield _sse({
        "type": "session_start",
        "session_id": request.session_id,
        "tenant_id": request.tenant_id,
    })

    max_tokens = tenant_config.get("max_tokens", {}).get("chat", _DEFAULT_MAX_TOKENS)
    messages = [m.model_dump() for m in request.messages]
    model_client = _get_model_client()

    turn = None
    async for event in model_client.run_turn(
        messages=messages, tools=[], max_tokens=max_tokens, system=_SYSTEM_PROMPT
    ):
        if event["type"] == "text_delta":
            yield _sse({"type": "token", "content": event["text"]})
        elif event["type"] == "turn_complete":
            turn = event["turn"]

    tokens_used = (turn.input_tokens + turn.output_tokens) if turn is not None else 0
    yield _sse({
        "type": "done",
        "tokens_used": tokens_used,
        "session_id": request.session_id,
    })
