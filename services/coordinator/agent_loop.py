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
import uuid
from datetime import datetime, timezone
from typing import AsyncGenerator, Optional

import httpx
from azure.cosmos.exceptions import CosmosResourceNotFoundError

from model_client import (
    FoundryModelClient,
    ModelBudgetExceededError,
    ModelClient,
    ModelRateLimitedError,
    ModelTurn,
    ModelUnavailableError,
    ToolCall,
)
from step_up import (
    StepUpResult,
    fetch_tool_credential,
    prepare_step_up,
    resolve_step_up,
)
from tools import TOOL_DEFINITIONS

logger = logging.getLogger(__name__)

# SSE keepalive interval (seconds) — sent during long approval polls to prevent ACA idle timeout
_KEEPALIVE_INTERVAL = 30

# Default max_tokens for a chat turn when tenant_config doesn't specify one.
_DEFAULT_MAX_TOKENS = 4096

# Guard against a runaway tool-call loop (e.g. Claude repeatedly re-calling a tool).
_MAX_TOOL_ROUNDS = 8

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
    result_holder: Optional[dict] = None,
) -> AsyncGenerator[str, None]:
    """
    Yields SSE strings for a single tool call, handling the step-up gate if required.
    Designed to be called directly from _stream_generator for step-up tools.
    Non-gated tools should use asyncio.as_completed() in the outer loop instead.

    result_holder, when provided, is populated with the tool outcome so the caller
    can build the tool_result Claude expects back: {"data": <agent response>} on
    success, or {"error": <str>} on rejection/expiry/credential failure/agent error.
    """
    policy = tenant_config.get("step_up_policy", {}).get(tool_name)

    if policy is None:
        # No gate — dispatch immediately (caller handles parallelism)
        data = await _call_agent(tool_name, tool_input, request)
        if result_holder is not None:
            result_holder["data"] = data
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
            if result_holder is not None:
                result_holder["error"] = "approval_rejected"
            return

        if step_up_result.status == "expired":
            yield _sse({
                "type": "approval_expired",
                "request_id": step_up_result.request_id,
                "tool": tool_name,
            })
            if result_holder is not None:
                result_holder["error"] = "approval_expired"
            return

        if step_up_result.status == "failed":
            yield _sse({
                "type": "agent_error",
                "agent": tool_name,
                "error": "credential_fetch_failed",
            })
            if result_holder is not None:
                result_holder["error"] = "credential_fetch_failed"
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
        data = await _call_agent(tool_name, tool_input, request, credential=credential)
        duration_ms = int((time.monotonic() - start) * 1000)
        yield _sse({"type": "agent_complete", "agent": tool_name, "duration_ms": duration_ms})
        if result_holder is not None:
            result_holder["data"] = data
    except Exception as exc:
        logger.error("Agent call failed", extra={"tool": tool_name}, exc_info=exc)
        yield _sse({"type": "agent_error", "agent": tool_name, "error": str(exc)})
        if result_holder is not None:
            result_holder["error"] = str(exc)


def _assistant_content_blocks(turn: ModelTurn) -> list[dict]:
    """Rebuilds the assistant content blocks Claude expects echoed back on the next turn."""
    blocks: list[dict] = []
    if turn.text:
        blocks.append({"type": "text", "text": turn.text})
    for call in turn.tool_calls:
        blocks.append({"type": "tool_use", "id": call.id, "name": call.name, "input": call.input})
    return blocks


def _tool_result_block(call: ToolCall, outcome: dict) -> dict:
    """Builds the tool_result content block fed back to Claude for one dispatched tool call."""
    if "error" in outcome:
        return {
            "type": "tool_result",
            "tool_use_id": call.id,
            "content": json.dumps({"error": outcome["error"]}),
            "is_error": True,
        }
    return {
        "type": "tool_result",
        "tool_use_id": call.id,
        "content": json.dumps(outcome.get("data", {})),
    }


async def _dispatch_tool_round(
    tool_calls: list[ToolCall],
    request,
    tenant_config: dict,
    results_out: list[dict],
) -> AsyncGenerator[str, None]:
    """
    Dispatches one Claude turn's worth of tool_use blocks and yields SSE strings as
    it goes. results_out is appended with one tool_result content block per call
    (order-independent — each block carries its own tool_use_id) for the caller to
    feed back to Claude as the next "user" turn.

    Step-up gated tools (tool name present in tenant_config["step_up_policy"]) run
    serially through the existing _stream_tool_call, forwarding its approval events.
    Non-gated tools are fanned out concurrently with asyncio.as_completed() so each
    agent_complete/agent_error is emitted the instant that agent finishes — not
    batched after the slowest (CLAUDE.md: never use asyncio.gather() here).
    """
    gated: list[ToolCall] = []
    non_gated: list[ToolCall] = []
    for call in tool_calls:
        policy = tenant_config.get("step_up_policy", {}).get(call.name)
        (gated if policy is not None else non_gated).append(call)

    # Gated tools run serially — human approval is inherently sequential.
    for call in gated:
        holder: dict = {}
        async for chunk in _stream_tool_call(call.name, call.input, request, tenant_config, result_holder=holder):
            yield chunk
        results_out.append(_tool_result_block(call, holder))

    if not non_gated:
        return

    # Emit every agent_start up front, in immediate succession, before any of them
    # begin executing — matches the README SSE state diagram.
    for call in non_gated:
        yield _sse({"type": "agent_start", "agent": call.name, "detail": call.input.get("device_host")})

    async def _run(call: ToolCall):
        start = time.monotonic()
        try:
            data = await _call_agent(call.name, call.input, request)
            return call, int((time.monotonic() - start) * 1000), data, None
        except Exception as exc:  # noqa: BLE001 — surfaced as agent_error, loop continues
            return call, None, None, exc

    tasks = [asyncio.ensure_future(_run(call)) for call in non_gated]
    for coro in asyncio.as_completed(tasks):
        call, duration_ms, data, exc = await coro
        if exc is None:
            yield _sse({"type": "agent_complete", "agent": call.name, "duration_ms": duration_ms})
            results_out.append(_tool_result_block(call, {"data": data}))
        else:
            logger.error("Agent call failed", extra={"tool": call.name}, exc_info=exc)
            yield _sse({"type": "agent_error", "agent": call.name, "error": str(exc)})
            results_out.append(_tool_result_block(call, {"error": str(exc)}))


def _derive_title(messages: list[dict]) -> str:
    """Best-effort session title from the first plain-text user message."""
    for m in messages:
        if m.get("role") == "user" and isinstance(m.get("content"), str):
            return m["content"][:60]
    return "New conversation"


async def _persist_conversation_and_audit(
    request, messages: list[dict], agents_invoked: list[str], tokens_used: int
) -> None:
    """
    Upserts the conversation document and writes an audit log entry to Cosmos DB.

    Called once, after the tool-use loop finishes and BEFORE `done` is emitted —
    CLAUDE.md: "Emit done before writing the Cosmos DB audit/conversation record"
    is listed under "What Claude Code Must Never Do". If either write raises, the
    caller's try/except turns that into an `error` SSE event and `done` is never
    sent — a completed interaction always has a corresponding audit log entry.

    `import main` is deferred (not module-level) to avoid a circular import —
    main.py imports `_stream_generator` from this module at import time. Mirrors
    the pattern already used in step_up.py's `_notify_out_of_band`.
    """
    import main as _main

    now_iso = datetime.now(timezone.utc).isoformat()

    try:
        doc = await _main.conversations_container.read_item(
            item=request.session_id, partition_key=request.tenant_id
        )
    except CosmosResourceNotFoundError:
        doc = {
            "id": request.session_id,
            "tenant_id": request.tenant_id,
            "title": _derive_title(messages),
            "created_at": now_iso,
            "agents": [],
        }

    doc["messages"] = messages
    doc["agents"] = sorted(set(doc.get("agents", [])) | set(agents_invoked))
    doc["updated_at"] = now_iso

    await _main.conversations_container.upsert_item(body=doc)

    audit_entry = {
        "id": f"audit-{uuid.uuid4()}",
        "tenant_id": request.tenant_id,
        "session_id": request.session_id,
        "timestamp": now_iso,
        "agents": doc["agents"],
        "tokens_used": tokens_used,
    }
    await _main.audit_logs_container.create_item(body=audit_entry)

    logger.info(
        "Conversation and audit log persisted",
        extra={
            "tenant_id": request.tenant_id,
            "session_id": request.session_id,
            "agents": doc["agents"],
            "tokens_used": tokens_used,
        },
    )


async def _stream_generator(request, tenant_config: dict) -> AsyncGenerator[str, None]:
    """
    Drives one POST /chat/stream request end to end: session_start -> Claude
    tool-use loop (gated tools serial, non-gated tools parallel) -> token* ->
    [Cosmos DB write] -> done, or -> error on a fatal failure.
    """
    yield _sse({
        "type": "session_start",
        "session_id": request.session_id,
        "tenant_id": request.tenant_id,
    })

    try:
        budget_remaining = tenant_config.get("token_budget_remaining")
        if budget_remaining is not None and budget_remaining <= 0:
            raise ModelBudgetExceededError("Tenant token budget exhausted")

        max_tokens = tenant_config.get("max_tokens", {}).get("chat", _DEFAULT_MAX_TOKENS)
        messages = [m.model_dump() for m in request.messages]
        model_client = _get_model_client()

        total_input_tokens = 0
        total_output_tokens = 0
        agents_invoked: list[str] = []

        for _round in range(_MAX_TOOL_ROUNDS):
            turn: Optional[ModelTurn] = None
            async for event in model_client.run_turn(
                messages=messages, tools=TOOL_DEFINITIONS, max_tokens=max_tokens, system=_SYSTEM_PROMPT
            ):
                if event["type"] == "text_delta":
                    yield _sse({"type": "token", "content": event["text"]})
                elif event["type"] == "turn_complete":
                    turn = event["turn"]

            if turn is None:
                raise ModelUnavailableError("model client did not yield a turn_complete event")

            total_input_tokens += turn.input_tokens
            total_output_tokens += turn.output_tokens
            messages.append({"role": "assistant", "content": _assistant_content_blocks(turn)})

            if not turn.tool_calls:
                break

            agents_invoked.extend(call.name for call in turn.tool_calls)
            results: list[dict] = []
            async for chunk in _dispatch_tool_round(turn.tool_calls, request, tenant_config, results):
                yield chunk
            messages.append({"role": "user", "content": results})
        else:
            logger.warning(
                "Tool round limit reached — returning with partial results",
                extra={"tenant_id": request.tenant_id, "session_id": request.session_id},
            )

        tokens_used = total_input_tokens + total_output_tokens

        # Cosmos DB write happens BEFORE done — audit integrity guarantee.
        await _persist_conversation_and_audit(request, messages, agents_invoked, tokens_used)

    except ModelBudgetExceededError as exc:
        logger.warning(
            "Token budget exceeded",
            extra={"tenant_id": request.tenant_id, "session_id": request.session_id},
        )
        yield _sse({"type": "error", "code": "budget_exceeded", "message": str(exc)})
        return
    except ModelRateLimitedError as exc:
        logger.warning(
            "Model provider rate limited the request",
            extra={"tenant_id": request.tenant_id, "session_id": request.session_id},
        )
        yield _sse({"type": "error", "code": "rate_limited", "message": str(exc)})
        return
    except Exception as exc:
        logger.error(
            "Fatal error in /chat/stream",
            extra={"tenant_id": request.tenant_id, "session_id": request.session_id},
            exc_info=exc,
        )
        yield _sse({
            "type": "error",
            "code": "coordinator_unavailable",
            "message": "Coordinator encountered an unexpected error",
        })
        return

    yield _sse({
        "type": "done",
        "tokens_used": tokens_used,
        "session_id": request.session_id,
    })
