# VIGIL — Step-Up Auth (Agent MFA)

**Date:** 2026-03-17
**Status:** Approved
**Scope:** `services/coordinator`, `infrastructure/terraform`, Cosmos DB, Azure Key Vault, Azure Communication Services

---

## Overview

Step-up auth is a generalised human-in-the-loop approval gate for high-risk coordinator tool calls. When Claude selects a tool marked as high-risk in the tenant's policy, the coordinator pauses the agentic loop, creates an approval request, notifies designated approvers (in-chat and/or out-of-band), and only proceeds after explicit human approval.

This mechanism replaces the bespoke approval handling in the existing two-phase network change flow. `apply_change` and `rollback_change` become the first tools governed by it. Future high-risk tools (bulk ITSM operations, user management, etc.) inherit the same pattern by adding an entry to `step_up_policy` in `tenant_config`.

---

## Architecture

### Lifecycle

```
Coordinator: Claude selects a high-risk tool
    ↓
Check active time-window grants (time_window tools only)
    → Valid grant found → Proceed directly to tool dispatch
    → No grant → Continue
    ↓
Create step_up_requests record (status: pending)
Dispatch out-of-band notifications if configured (fire-and-forget)
    ↓ SSE: approval_required (request_id, tool, context, approver_type, expires_at)
    ↓
[Human approves or rejects — in-chat or out-of-band]
    ↓
POST /step-up/{request_id}/approve  (authenticated, authorisation-checked)
    ↓
Cosmos DB: step_up_requests status → approved
    ↓
Coordinator: polling detects approval
Coordinator: fetch sensitive credential from Key Vault (just-in-time)
    ↓ SSE: approval_granted (request_id, tool, approved_by)
    ↓
Coordinator: dispatch tool call
    ↓
(time_window tools only) Write active grant to step_up_grants container
```

### What changes vs. today

- `apply_change` and `rollback_change` approval logic is migrated from bespoke coordinator code onto this mechanism
- `POST /changes/{change_id}/apply` is replaced by `POST /step-up/{request_id}/approve`
- Key Vault write credentials are fetched just-in-time after approval — not cached at startup
- The Network Agent's internal guard (`if record["status"] != "approved": raise`) is retained as defence-in-depth
- `change_records` is retained — it records the full change lifecycle (diff, pre-change config, commands, rollback). `approved_by` is written to both `step_up_requests` and `change_records`

---

## Data Model

### `step_up_requests` — new Cosmos DB container

Partitioned by `tenant_id`, keyed by `request_id`.

```python
{
    "id": "sur-{uuid}",
    "tenant_id": "tenant-a",
    "session_id": "s-abc123",
    "tool_name": "apply_change",
    "requested_by": "jsmith@client.com",       # from SAML claims
    "requested_at": "2026-03-17T10:00:00Z",
    "status": "pending",                        # pending | approved | rejected | expired | failed
    "context": {                                # tool-defined summary shown to approver
        "change_id": "chg-001",
        "device_host": "10.0.0.1",
        "summary": "Shut down interface Gi0/1"
    },
    "approved_by": null,
    "approved_at": null,
    "expires_at": "2026-03-17T10:15:00Z",      # pending window expiry
    "grant_type": "single_use",                # single_use | time_window
    "grant_duration_seconds": null             # populated for time_window grants
}
```

**Status machine:**

```
pending → approved → (tool dispatched)
        → rejected
        → expired   (expires_at passed without decision)
        → failed    (approved but Key Vault unreachable)
```

### `step_up_grants` — new Cosmos DB container

Active time-window grants. Partitioned by `tenant_id`. Cosmos DB TTL set to `grant_duration_seconds` — documents auto-expire.

```python
{
    "id": "grnt-{uuid}",
    "tenant_id": "tenant-a",
    "session_id": "s-abc123",
    "tool_name": "bulk_close_tickets",
    "approved_by": "senior-eng@client.com",
    "granted_at": "2026-03-17T10:01:00Z",
    "expires_at": "2026-03-17T10:31:00Z",
    "_ttl": 1800                               # Cosmos DB TTL in seconds
}
```

### `tenant_config` additions

```python
{
    "tenant_id": "tenant-a",
    # ... existing fields ...
    "step_up_policy": {
        "apply_change": {
            "risk_level": "high",
            "self_approve": false,             # separate approver required
            "grant_type": "single_use",
            "pending_ttl_seconds": 900,        # 15 min to approve before expiry
            "notification_channels": ["in_chat", "email"]
        },
        "rollback_change": {
            "risk_level": "high",
            "self_approve": false,
            "grant_type": "single_use",
            "pending_ttl_seconds": 900,
            "notification_channels": ["in_chat", "email"]
        },
        "bulk_close_tickets": {
            "risk_level": "low",
            "self_approve": true,              # requesting user can self-approve
            "grant_type": "time_window",
            "grant_duration_seconds": 1800,    # 30 min window once approved
            "pending_ttl_seconds": 300,
            "notification_channels": ["in_chat"]
        }
    },
    "step_up_approvers": [
        "senior-eng@client.com",
        "change-manager@client.com"
    ],
    "step_up_notification_email": "approvals@client.com",
    "step_up_webhook_url": null                # optional webhook for out-of-band
}
```

Tools absent from `step_up_policy` have no gate and are dispatched immediately.

---

## Coordinator Changes

### New file: `services/coordinator/step_up.py`

Owns the full step-up lifecycle: policy lookup, request creation, polling, grant management, and Key Vault fetch.

### Agentic loop modification — `services/coordinator/agent_loop.py`

Before dispatching any tool call:

```python
async def _dispatch_tool_call(tool_name, tool_input, request, tenant_config):
    policy = tenant_config.get("step_up_policy", {}).get(tool_name)

    if policy is None:
        return await _call_agent(tool_name, tool_input, request)

    if policy["grant_type"] == "time_window":
        grant = await get_active_grant(request.tenant_id, request.session_id, tool_name)
        if grant:
            return await _call_agent(tool_name, tool_input, request)

    step_up_req = await create_step_up_request(tool_name, tool_input, request, policy)

    if "email" in policy["notification_channels"]:
        await notify_approvers(step_up_req, tenant_config)  # fire-and-forget

    yield _sse({
        "type": "approval_required",
        "request_id": step_up_req["id"],
        "tool": tool_name,
        "context": step_up_req["context"],
        "approver_type": "self" if policy["self_approve"] else "designated",
        "expires_at": step_up_req["expires_at"],
    })

    decision = await await_step_up_decision(step_up_req["id"], request.tenant_id, policy)

    if decision["status"] in ("rejected", "expired"):
        yield _sse({"type": f"approval_{decision['status']}",
                    "request_id": step_up_req["id"], "tool": tool_name,
                    "decided_by": decision.get("approved_by")})
        return None  # graceful degradation — coordinator continues with partial results

    try:
        credential = await fetch_tool_credential(tool_name, request.tenant_id)
    except Exception as e:
        await mark_step_up_failed(step_up_req["id"], request.tenant_id)
        yield _sse({"type": "agent_error", "agent": tool_name, "error": "credential_fetch_failed"})
        return None

    if policy["grant_type"] == "time_window":
        await write_active_grant(tool_name, decision["approved_by"], request, policy)

    yield _sse({"type": "approval_granted", "request_id": step_up_req["id"],
                "tool": tool_name, "approved_by": decision["approved_by"]})

    return await _call_agent(tool_name, tool_input, request, credential=credential)
```

### Polling strategy

`await_step_up_decision` polls Cosmos DB with exponential backoff (1s → 2s → 4s → max 10s) until status changes or `expires_at` is reached. Uses partition key `(request_id, tenant_id)` on every read.

### Startup recovery task

On startup and every 5 minutes, the coordinator scans `step_up_requests` for records stuck in `pending` with `expires_at` in the past and marks them `expired`. Mirrors the existing `applying_started_at` recovery for `change_records`.

```python
stuck = container.query_items(
    query="SELECT * FROM c WHERE c.status = 'pending' AND c.expires_at < @now",
    parameters=[{"name": "@now", "value": now_iso}],
    partition_key=tenant_id  # per-tenant scan
)
for record in stuck:
    record["status"] = "expired"
    container.replace_item(item=record["id"], body=record, partition_key=tenant_id)
```

### New endpoints — `services/coordinator/main.py`

```
POST /step-up/{request_id}/approve
POST /step-up/{request_id}/reject
```

Both require a valid ISE token (enforced at Gateway). The coordinator checks:
1. `request_id` exists and belongs to caller's `tenant_id`
2. `status == "pending"` and `expires_at` not passed
3. If `self_approve: false` — caller is in `step_up_approvers` AND is not `requested_by`
4. If `self_approve: true` — caller is `requested_by` OR is in `step_up_approvers`

Violations return structured 403 responses with `reason` field. All attempts (approved, rejected, and forbidden) are written to the audit log.

---

## Notification Helper — `services/coordinator/notifications.py`

Fire-and-forget. Sends approval requests to configured out-of-band channels. Notification failure is logged and never blocks the in-chat approval path.

```python
async def notify_approvers(step_up_request: dict, tenant_config: dict):
    approval_url = f"{GATEWAY_EXTERNAL_URL}/step-up/{step_up_request['id']}/approve"
    reject_url   = f"{GATEWAY_EXTERNAL_URL}/step-up/{step_up_request['id']}/reject"

    body = {
        "tool":         step_up_request["tool_name"],
        "requested_by": step_up_request["requested_by"],
        "context":      step_up_request["context"],
        "expires_at":   step_up_request["expires_at"],
        "approve_url":  approval_url,
        "reject_url":   reject_url,
    }

    if tenant_config.get("step_up_notification_email"):
        await _send_email(tenant_config["step_up_notification_email"], body)

    if tenant_config.get("step_up_webhook_url"):
        await _post_webhook(tenant_config["step_up_webhook_url"], body)
```

Email is sent via Azure Communication Services (Managed Identity — no new credentials). Webhook is a plain `httpx` POST with a single retry.

---

## Key Vault Integration

Write credentials are fetched just-in-time after approval, not at startup. Secret naming follows the existing namespace convention:

```
tenant-{tenant_id}-{tool_name}-write-credential
```

Example: `tenant-acme-apply_change-write-credential`

The coordinator does not cache these secrets in module scope. Each approved invocation performs a fresh Key Vault read. This ensures that revoking the secret in Key Vault takes effect immediately without a container restart.

---

## SSE Events

### New events

| Event | Fields | When emitted |
|---|---|---|
| `approval_required` | `request_id`, `tool`, `context`, `approver_type`, `expires_at` | Loop paused, awaiting decision |
| `approval_granted` | `request_id`, `tool`, `approved_by` | Decision received, tool about to dispatch |
| `approval_rejected` | `request_id`, `tool`, `decided_by` | Approver rejected — coordinator continues with partial results |
| `approval_expired` | `request_id`, `tool` | `expires_at` passed without decision |

### Updated event table (full)

| Event | Fields | Notes |
|---|---|---|
| `session_start` | `session_id`, `tenant_id` | Unchanged |
| `agent_start` | `agent`, `detail` | Unchanged |
| `agent_complete` | `agent`, `duration_ms` | Unchanged |
| `agent_error` | `agent`, `error` | Unchanged |
| `approval_required` | `request_id`, `tool`, `context`, `approver_type`, `expires_at` | New |
| `approval_granted` | `request_id`, `tool`, `approved_by` | New |
| `approval_rejected` | `request_id`, `tool`, `decided_by` | New |
| `approval_expired` | `request_id`, `tool` | New |
| `token` | `content` | Unchanged |
| `done` | `tokens_used`, `session_id` | Unchanged |
| `error` | `code`, `message` | Unchanged |

---

## Error Handling

| Scenario | Behaviour |
|---|---|
| Approval expires | `approval_expired` SSE; coordinator treats as partial result |
| Approver rejects | `approval_rejected` SSE; graceful degradation |
| Coordinator restarts mid-poll | Startup recovery task expires stale `pending` records; UI surfaces timeout |
| Caller not in `step_up_approvers` | 403 `reason: not_authorised_approver`; logged to audit |
| Self-approval attempt on `self_approve: false` tool | 403 `reason: self_approval_not_permitted`; logged to audit |
| Key Vault unreachable after approval | `step_up_requests` status → `failed`; `agent_error` SSE; approvers notified |
| Notification delivery failure | Logged, not fatal; in-chat path always available |
| Tool absent from `step_up_policy` | No gate; dispatched immediately |
| Duplicate approval attempt | 409 `reason: already_decided`; idempotent |

---

## Multi-Tenancy

All existing multi-tenancy rules apply. Additions:

| Resource | Isolation mechanism |
|---|---|
| `step_up_requests` | Partitioned by `tenant_id`; all lookups use `(request_id, tenant_id)` |
| `step_up_grants` | Partitioned by `tenant_id` |
| `step_up_policy` | Per-tenant in `tenant_config` |
| `step_up_approvers` | Per-tenant list; cross-tenant approval is impossible by construction |
| Key Vault secrets | Namespaced `tenant-{tenant_id}-{tool}-write-credential` |
| Audit log entries | Include `tenant_id`, `tool_name`, `requested_by`, `approved_by`, `request_id` |

---

## Files Changed

| File | Type | Purpose |
|---|---|---|
| `services/coordinator/step_up.py` | New | Step-up lifecycle: request creation, polling, grant management, Key Vault fetch |
| `services/coordinator/notifications.py` | New | Out-of-band notification helper (email + webhook) |
| `services/coordinator/agent_loop.py` | Modified | Pre-dispatch step-up check; SSE events for approval flow |
| `services/coordinator/main.py` | Modified | `POST /step-up/{id}/approve` and `POST /step-up/{id}/reject` endpoints |
| `infrastructure/terraform/modules/cosmos-db/main.tf` | Modified | Add `step_up_requests` and `step_up_grants` containers with TTL |
| `ARCHITECTURE.md` | Modified | Document step-up auth in Security Model and Data Flow sections |

---

## Out of Scope

- Push notifications to mobile devices (webhook covers custom integrations)
- Mid-stream cancellation of an in-progress tool call once dispatched
- UI approval modal design (follows existing change approval modal pattern)
- Per-request risk scoring (policy is static per tool per tenant)
- Approval delegation (approver cannot delegate to another user)
