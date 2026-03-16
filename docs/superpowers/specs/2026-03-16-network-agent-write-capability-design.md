# VIGIL — Network Agent Write Capability + Change Reviewer Agent

**Date:** 2026-03-16
**Status:** Approved
**Scope:** `services/agent-network`, `services/agent-change-reviewer`, `services/coordinator`, `services/ui`, Cosmos DB, ISE TACACS+, Terraform

---

## Overview

Extends the Network Agent from read-only to read-write, enabling configuration and state changes to network devices. A new specialist agent — the Change Reviewer Agent — performs AI peer review of every proposed change before it is presented to the user. All changes require explicit human approval via a modal UI. Every applied change is backed by a pre-change config capture enabling full rollback. Every applied, failed, or rolled-back change automatically raises a Jira ticket.

---

## Architecture

### Two-phase change flow

Changes follow a strict two-phase pattern:

**Phase 1 — Propose and Review (no device changes)**
1. Coordinator calls Network Agent (`propose_change`) — connects to device, captures current config, generates diff, writes `pending` change record to Cosmos DB. Nothing is pushed to the device.
2. Coordinator calls Change Reviewer Agent — analyses the proposed change for correctness, risk, and alternatives. Returns a structured recommendation. Updates change record to `reviewed`.
3. Coordinator streams `change_proposed` and `change_reviewed` SSE events to UI.
4. UI renders approval modal with diff and peer review. User approves or rejects.

**Phase 2 — Apply (only after explicit approval)**
1. UI sends `POST /changes/{change_id}/apply` to the Coordinator with `tenant_id` from session. The Coordinator validates the change record belongs to the requesting tenant, then sets `status → "approved"` and `approved_by` (extracted from SAML token claims) in the change record before calling the Network Agent.
2. Coordinator calls Network Agent (`apply_change`) — fetches `change_commands` from the stored change record (never from the tool call input), validates `status == "approved"` and record is not expired, compares current device config against `pre_change_config` for the affected sections, and pushes config to device.
3. If drift is detected, the Network Agent emits `change_drift_detected`, sets `status → "drift_pending"`, and returns. The Phase 2 SSE stream closes normally with `done`. The UI renders a drift re-confirmation modal. The user has two options:
   - **Acknowledge:** UI sends `POST /changes/{change_id}/acknowledge-drift` → Coordinator sets `drift_acknowledged: true`, refreshes `expires_at` by 30 minutes, and sets `status → "approved"` on the change record, then opens a **new SSE stream** for the resumed apply. On the resumed call the Network Agent sees `drift_acknowledged: true` and skips the drift check.
   - **Cancel:** UI sends `POST /changes/{change_id}/abort` → Coordinator sets `status → "failed"`. No further apply possible for this change ID.
4. Coordinator calls ITSM Agent — raises Jira ticket with change details and outcome.
5. Coordinator streams `change_applying`, `change_applied` or `change_failed` SSE event.

### Write capability is per-tenant

Write operations are only available for tenants with `write_enabled: true` in their `tenant_config` document in Cosmos DB. The Network Agent checks this before executing any write operation. A tenant without write enabled cannot trigger `propose_change`, `apply_change`, or `rollback_change`.

---

## Components

### 1. Network Agent — `services/agent-network`

#### Updated tool schema

The `query_type` field is replaced with `operation`:

```python
{
    "name": "network_agent",
    "description": "Connects to network devices to retrieve configuration and state, propose changes, apply approved changes, and rollback changes. Use for all device interrogation and configuration operations.",
    "input_schema": {
        "type": "object",
        "properties": {
            "device_host": {
                "type": "string",
                "description": "IP address or hostname of the target device"
            },
            "operation": {
                "type": "string",
                "enum": [
                    "running_config", "interfaces", "routing_table", "acl",
                    "propose_change", "apply_change", "rollback_change"
                ]
            },
            "change_commands": {
                "type": "string",
                "description": "Required for propose_change only. The configuration or state commands to apply, one per line. Ignored at apply time — apply always uses commands from the stored change record."
            },
            "change_id": {
                "type": "string",
                "description": "Required for apply_change and rollback_change."
            }
        },
        "required": ["device_host", "operation"]
    }
}
```

#### Operation behaviour

**`propose_change`**
- Check `write_enabled` in tenant config — return structured error `write_capability_not_enabled` if not set
- Connect to device via ISE TACACS+ (read profile)
- Capture full running config
- Generate diff between current config and `change_commands`
- Write change record to Cosmos DB with `status: "pending"`, `proposed_change`, `pre_change_config`, `expires_at: now + 30 minutes`
- Return `change_id` and diff — nothing is sent to the device

**`apply_change`**
- Fetch change record from Cosmos DB using `(change_id, tenant_id)` as partition key — reject if `tenant_id` on the record does not match the request `tenant_id`
- Reject if `status != "approved"` — returns `error: invalid_change_status`. Records in `drift_pending` are caught by this guard — `POST /changes/{change_id}/acknowledge-drift` must be called first to transition `drift_pending → approved` before a resumed apply is accepted.
- Reject if `expires_at < now` — returns `error: change_record_expired`
- Fetch `change_commands` from the stored change record — **never from the tool call input**. The Coordinator's tool call input for `change_commands` is ignored at apply time. This ensures only the reviewed and approved commands are executed.
- Update `status → "applying"`
- Connect to device via ISE TACACS+ (write profile)
- Check `drift_acknowledged` flag on the change record. If `false` (default): compare current running config against stored `pre_change_config` for the sections touched by `change_commands`. If drift is detected, emit `change_drift_detected` SSE event, set `status → "drift_pending"`, and return — apply does not proceed. If `drift_acknowledged` is `true`, skip the drift check and proceed directly to pushing commands.
- Push `change_commands` to device
- Verify change was applied (re-read affected config sections)
- Update change record: `status → "applied"`, `applied_at`, `apply_result: "success"`
- Return result

**`rollback_change`**
- Fetch change record using `(change_id, tenant_id)` as partition key
- Validate `status` is `"applied"` or `"failed"` — reject with `error: invalid_change_status` for any other status. This prevents rolling back a pending proposal, double-rolling-back a `rolled_back` record, or rolling back a `rejected` change.
- Retrieve `pre_change_config` from change record
- Connect to device via ISE TACACS+ (write profile)
- Push `pre_change_config` as config replace
- Verify rollback applied
- Update change record: `status → "rolled_back"`, `rolled_back_at`
- Return result

#### Stuck `applying` recovery

If the Network Agent crashes or loses connectivity after setting `status → "applying"` but before completing, the record becomes stuck. The change record stores an `applying_started_at` timestamp written at the moment `status → "applying"` is set. A background task in the Network Agent checks Cosmos DB on startup and periodically for records in `applying` status where `applying_started_at < now - 10 minutes` (per tenant), and transitions them to `failed` with `failure_reason: "apply_timeout"`. Using the Cosmos DB timestamp (not in-memory state) ensures the check survives container restarts — the task correctly identifies stuck records even after the container that caused the stuck state has restarted.

---

### 2. Change Reviewer Agent — `services/agent-change-reviewer`

New FastAPI container. Uses Claude with a network engineer reviewer system prompt focused on correctness, risk assessment, and alternatives.

**Data handling:** `current_config` and `proposed_change` are used only during the review Claude call. They are never written to logs, audit records, or stored in Cosmos DB. Only the structured `ChangeReview` response is persisted. Log entries for reviewer calls must omit these field values.

#### Tool registration

```python
{
    "name": "change_reviewer_agent",
    "description": "Performs AI peer review of a proposed network change. Assesses correctness, risk, blast radius, and suggests alternatives. Always call after propose_change and before presenting the change to the user.",
    "input_schema": {
        "type": "object",
        "properties": {
            "change_id": {"type": "string"},
            "device_type": {
                "type": "string",
                "description": "Platform type e.g. cisco_ios, cisco_nxos, palo_alto_panos"
            },
            "proposed_change": {"type": "string"},
            "current_config": {
                "type": "string",
                "description": "Current device running config. Never logged or stored by the reviewer — used only for the review Claude call."
            }
        },
        "required": ["change_id", "device_type", "proposed_change", "current_config"]
    }
}
```

Note: `device_host` is intentionally excluded from the schema. The reviewer assesses the change against the config content and device platform type — the IP address provides no additional review context.

#### Response schema

```python
class ChangeReview(BaseModel):
    tenant_id: str
    change_id: str
    correctness: str              # syntax validity, logical correctness assessment
    risk: str                     # blast radius, affected services, severity
    alternatives: str | None      # better approaches if they exist, null if none
    recommendation: Literal["approve", "flag", "reject"]
    recommendation_reason: str
    error: str | None = None
```

**Recommendation meanings:**
- `approve` — change is correct and low risk
- `flag` — change is technically valid but has risks the human should consciously accept
- `reject` — change is incorrect, dangerous, or should not be applied as written

The reviewer updates the change record in Cosmos DB: sets `review` field and transitions `status: "pending" → "reviewed"`. The `status` field stays in `pending` until the full review is written atomically as `reviewed` — there is no intermediate `reviewing` database status. The `change_reviewing` SSE event indicates the reviewer is in progress at the stream layer only.

---

### 3. Cosmos DB — `change_records` Container

New container, partitioned by `tenant_id`.

```python
{
    "id": "change-uuid",
    "tenant_id": "tenant-a",
    "session_id": "s-abc123",
    "device_host": "10.0.0.1",
    "device_type": "cisco_ios",
    "change_type": "config | state",
    "change_commands": "interface GigabitEthernet0/1\n shutdown",
    "proposed_change": "<diff>",
    "pre_change_config": "<full running config>",
    "proposed_at": "2026-03-16T10:00:00Z",
    "expires_at": "2026-03-16T10:30:00Z",
    "review": {
        "correctness": "Valid. Achieves stated intent.",
        "risk": "Shuts access port. Hosts on this segment will lose connectivity.",
        "alternatives": "Consider err-disable recovery before shutdown.",
        "recommendation": "flag",
        "recommendation_reason": "Valid but impactful — confirm host impact is acceptable."
    },
    "status": "pending | reviewed | approved | applying | applied | failed | rolled_back | rejected",
    "drift_acknowledged": false,
    "applying_started_at": null,
    "approved_by": "user@tenant.com",
    "approved_at": "2026-03-16T10:02:00Z",
    "applied_at": "2026-03-16T10:02:05Z",
    "apply_result": "success | failure",
    "failure_reason": null,
    "jira_ticket": "VIGIL-124",
    "rolled_back_at": null
}
```

**Write ordering guarantee:** `pre_change_config` is written to the record before any commands are sent to the device. If the apply fails mid-push, rollback always has a clean baseline.

**TTL / expiry:** Change records in `pending` or `reviewed` status have an `expires_at` field set to `proposed_at + 30 minutes` (configurable per tenant in `tenant_config`). At `apply_change`, the Network Agent rejects the request if `expires_at < now`. This prevents applying a stale proposal against a device whose config may have changed significantly. The 30-minute default is configurable. Terminal states (`applied`, `rolled_back`, `rejected`, `failed`) do not expire — they are permanent audit records.

**State machine:**

```
pending → reviewed → approved → applying → applied
                              ↘ rejected  ↓          ↘ failed → rolled_back
                                          ↓
                                    drift_pending → approved (drift_acknowledged: true) → applying → ...
                                          ↘ failed (user cancelled drift)
```

Note: `drift_pending` is entered from `applying` — the `applying` status is set first, then drift is detected, then the record transitions to `drift_pending`. After acknowledgement, `status → "approved"` and a new `applying` cycle begins.

`apply_change` validates `status == "approved"` and `expires_at > now` before proceeding — prevents replay, double-application, and stale proposals.

---

### 4. Phase 2 Request

The UI sends a dedicated request to apply or reject an approved change — separate from the normal chat stream:

```
POST /changes/{change_id}/apply
POST /changes/{change_id}/reject
```

All four change action endpoints are on the Coordinator (proxied through the Gateway with normal auth/rate-limit checks):

```
POST /changes/{change_id}/apply
POST /changes/{change_id}/reject
POST /changes/{change_id}/acknowledge-drift
POST /changes/{change_id}/abort
```

All four use the same request model:

```python
class ChangeActionRequest(BaseModel):
    tenant_id: str    # from session, validated by Gateway
    session_id: str   # to continue the correct conversation stream
```

`tenant_id` is extracted from the SAML token by the Gateway and injected — the UI never sets it directly. The Coordinator validates that the change record's `tenant_id` matches before proceeding.

Both endpoints return `text/event-stream` — the apply path streams `change_applying` → `change_applied`/`change_failed` → `done`. The reject path streams a confirmation token and `done`.

---

### 5. SSE Events — New Types

Eight new event types added to the existing schema:

| Event | Key Fields | When emitted |
|---|---|---|
| `change_proposed` | `change_id`, `device_host`, `diff`, `change_type` | Network Agent returns proposal |
| `change_reviewing` | `change_id` | Change Reviewer Agent starts (stream layer only — no DB status change) |
| `change_reviewed` | `change_id`, `recommendation`, `correctness`, `risk`, `alternatives`, `recommendation_reason` | Reviewer returns result |
| `change_drift_detected` | `change_id`, `drift_diff` | Drift found in affected config sections at apply time — apply continues |
| `change_applying` | `change_id` | User approved, apply phase begins |
| `change_applied` | `change_id`, `jira_ticket`, `duration_ms` | Change verified on device |
| `change_failed` | `change_id`, `error`, `jira_ticket` | Apply or rollback failed — Jira ticket always raised |
| `change_rolled_back` | `change_id`, `jira_ticket`, `duration_ms` | Rollback complete |

#### Full stream trace — successful flagged change

```
data: {"type": "session_start", "session_id": "s-abc123", "tenant_id": "tenant-a"}

data: {"type": "agent_start", "agent": "network_agent", "detail": "10.0.0.1"}
data: {"type": "change_proposed", "change_id": "c-xyz", "device_host": "10.0.0.1", "diff": "+ interface Gi0/1\n+  shutdown", "change_type": "config"}
data: {"type": "agent_complete", "agent": "network_agent", "duration_ms": 1100}

data: {"type": "agent_start", "agent": "change_reviewer_agent", "detail": null}
data: {"type": "change_reviewing", "change_id": "c-xyz"}
data: {"type": "change_reviewed", "change_id": "c-xyz", "recommendation": "flag", "correctness": "Valid. Achieves stated intent.", "risk": "Hosts on this segment lose connectivity.", "alternatives": "Consider err-disable recovery first.", "recommendation_reason": "Valid but impactful."}
data: {"type": "agent_complete", "agent": "change_reviewer_agent", "duration_ms": 2200}

data: {"type": "token", "content": "I've proposed shutting down Gi0/1. The peer review has flagged a risk..."}
data: {"type": "done", "tokens_used": 640, "session_id": "s-abc123"}

--- user approves in modal ---

data: {"type": "session_start", "session_id": "s-abc123", "tenant_id": "tenant-a"}
data: {"type": "agent_start", "agent": "network_agent", "detail": "10.0.0.1"}
data: {"type": "change_applying", "change_id": "c-xyz"}
data: {"type": "change_applied", "change_id": "c-xyz", "jira_ticket": "VIGIL-124", "duration_ms": 980}
data: {"type": "agent_complete", "agent": "network_agent", "duration_ms": 980}

data: {"type": "agent_start", "agent": "itsm_agent", "detail": null}
data: {"type": "agent_complete", "agent": "itsm_agent", "duration_ms": 440}

data: {"type": "token", "content": "Change applied successfully. Jira ticket VIGIL-124 raised."}
data: {"type": "done", "tokens_used": 280, "session_id": "s-abc123"}
```

#### Stream trace — drift detected and acknowledged

```
--- Phase 2: user approves, drift detected ---

data: {"type": "session_start", "session_id": "s-abc123", "tenant_id": "tenant-a"}
data: {"type": "agent_start", "agent": "network_agent", "detail": "10.0.0.1"}
data: {"type": "change_applying", "change_id": "c-xyz"}
data: {"type": "change_drift_detected", "change_id": "c-xyz", "drift_diff": "- ip access-list extended OUTSIDE-IN\n-  permit tcp any any eq 443"}
data: {"type": "agent_complete", "agent": "network_agent", "duration_ms": 820}
data: {"type": "token", "content": "Config drift detected on 10.0.0.1 since the change was proposed..."}
data: {"type": "done", "tokens_used": 210, "session_id": "s-abc123"}

--- UI renders drift re-confirmation modal ---
--- user clicks "Acknowledge & Continue" ---
--- UI sends POST /changes/c-xyz/acknowledge-drift ---
--- Coordinator sets drift_acknowledged: true, status → "approved" ---

--- New SSE stream opens ---

data: {"type": "session_start", "session_id": "s-abc123", "tenant_id": "tenant-a"}
data: {"type": "agent_start", "agent": "network_agent", "detail": "10.0.0.1"}
data: {"type": "change_applying", "change_id": "c-xyz"}
data: {"type": "change_applied", "change_id": "c-xyz", "jira_ticket": "VIGIL-125", "duration_ms": 950}
data: {"type": "agent_complete", "agent": "network_agent", "duration_ms": 950}
data: {"type": "agent_start", "agent": "itsm_agent", "detail": null}
data: {"type": "agent_complete", "agent": "itsm_agent", "duration_ms": 410}
data: {"type": "token", "content": "Change applied successfully despite config drift. Jira ticket VIGIL-125 raised."}
data: {"type": "done", "tokens_used": 295, "session_id": "s-abc123"}
```

---

### 6. UI — Change Approval Modal

When `change_reviewed` arrives, the UI renders a full-screen modal overlay (chat dimmed behind it). The modal contains:

- Device host and change type
- Proposed diff (syntax highlighted)
- Peer review panel: correctness, risk, alternatives, recommendation reason
- Recommendation badge: green (`approve`), amber (`flag`), red (`reject`)
- **Approve & Apply** button — rendered only if recommendation is `approve` or `flag`
- **Reject** button — always rendered

On approval: UI sends `POST /changes/{change_id}/apply`. On rejection: UI sends `POST /changes/{change_id}/reject`.

**After `change_failed`:** Modal re-appears with a **Rollback** button. If rollback also fails, the Rollback button is disabled, a critical warning is displayed with the Jira ticket reference (e.g. "VIGIL-125 — Manual intervention required"), and no further automated action is available. This is the terminal UI failure state.

**After `change_drift_detected` (Phase 2):** The original approval modal is replaced with a drift re-confirmation modal showing the drifted config sections alongside the original proposal. The user must explicitly acknowledge the drift — clicking "Acknowledge & Continue" sends `POST /changes/{change_id}/acknowledge-drift`. If the user cancels, clicking "Abort" sends `POST /changes/{change_id}/abort` and `status → "failed"`.

---

### 7. ITSM Agent — Change Tickets

The ITSM Agent is called after every `apply_change`, `failed` apply, and `rollback_change`. Jira ticket creation failure is **non-fatal** — a `agent_error` is emitted and the change flow continues. The change record already contains the complete audit trail.

**Ticket fields:**

| Field | Value |
|---|---|
| Summary | `[VIGIL] {change_type} change on {device_host} — {status}` |
| Description | Device host, change commands, peer review recommendation, approved by, applied at, outcome |
| Priority | Normal (applied), High (failed), Critical (rollback failed) |
| Labels | `vigil`, `tenant-{tenant_id}`, `network-change` |
| Custom field | `tenant_id` reference for billing and cross-tenant reporting |

---

### 8. TACACS+ Configuration

Two command sets per tenant in ISE:

**Read profile (existing)**
```
permit: show *
deny:   *
```

**Write profile (new)**
```
permit: show *
permit: configure terminal
permit: interface *
permit: shutdown
permit: no shutdown
permit: ip access-list *
permit: ip route *
permit: write memory
deny:   reload
deny:   erase
deny:   delete
deny:   *
```

The write profile is only activated per tenant when `write_enabled: true` in `tenant_config`. The catch-all deny ensures any command not explicitly listed is blocked at the ISE level — independent of application code.

---

## Error Handling

| Scenario | Behaviour |
|---|---|
| `write_enabled` not set | Network Agent returns `error: write_capability_not_enabled` before device connection |
| Reviewer returns `reject` | Modal shows rejection reason, Approve button hidden |
| User rejects in modal | `status → rejected`, `POST /changes/{change_id}/reject`, no device connection |
| Change record expired (`expires_at` passed) | `apply_change` returns `error: change_record_expired` |
| Record stuck in `applying` > 10 min | Background task transitions to `failed`, surfaces on next session interaction |
| Drift detected in affected sections | `change_drift_detected` SSE event, user must acknowledge before approving |
| Apply fails mid-push | `status → failed`, `change_failed` SSE event with `jira_ticket`, modal shows Rollback button |
| Rollback fails | `change_failed` SSE event, Rollback button disabled, critical Jira ticket reference shown, human intervention required |
| TACACS+ denies command | Network Agent returns structured error, `status → failed` |
| ITSM Agent fails to create ticket | `agent_error` emitted, change flow continues — change record is authoritative audit trail |
| `change_id` tenant mismatch | `apply_change` / `rollback_change` rejects with `error: unauthorized` |

---

## Multi-Tenancy

All existing multi-tenancy rules apply. Additional rules for write capability:

- `write_enabled` flag is per-tenant in `tenant_config` Cosmos DB container
- All `change_records` lookups use `(change_id, tenant_id)` — `tenant_id` always as partition key
- `apply_change` and `rollback_change` validate `tenant_id` on the record matches the request before executing
- TACACS+ write profiles are scoped per tenant — tenant A's write access cannot reach tenant B's devices
- `current_config` and `proposed_change` are never logged or stored by the Change Reviewer Agent
- Jira tickets include `tenant_id` label for cross-reference

---

## Files Changed

All service directories are new (greenfield). All files listed are to be created.

| File | Purpose |
|---|---|
| `services/agent-network/main.py` | FastAPI app with read + write operations |
| `services/agent-network/connectors/cisco_ios.py` | Netmiko connector — read and write |
| `services/agent-network/connectors/cisco_nxos.py` | NX-OS connector |
| `services/agent-network/connectors/palo_alto.py` | PAN-OS connector |
| `services/agent-network/tacacs/ise_auth.py` | ISE TACACS+ auth — read and write profiles |
| `services/agent-change-reviewer/main.py` | FastAPI app, review endpoint |
| `services/agent-change-reviewer/reviewer.py` | Claude-powered review logic |
| `services/agent-change-reviewer/requirements.txt` | Dependencies |
| `services/agent-change-reviewer/Dockerfile` | Standard Python 3.11 container |
| `services/coordinator/main.py` | Add `POST /changes/{change_id}/apply`, `/reject`, `/acknowledge-drift`, and `/abort` endpoints |
| `services/coordinator/tools/network.py` | Updated tool definition |
| `services/coordinator/tools/change_reviewer.py` | New tool definition |
| `services/ui/src/components/ChangeApprovalModal.tsx` | Modal with diff, review, approve/reject, drift re-confirmation, rollback failure state |
| `.github/workflows/deploy-agent-network.yml` | CI/CD workflow for Network Agent |
| `.github/workflows/deploy-agent-change-reviewer.yml` | CI/CD workflow for Change Reviewer Agent |
| `infrastructure/terraform/modules/cosmos-db/main.tf` | Add `change_records` container with `tenant_id` partition key and TTL policy |

**New environment variables:**

| Service | Variable | Value |
|---|---|---|
| Coordinator | `CHANGE_REVIEWER_AGENT_URL` | Internal URL of the Change Reviewer Agent |
| Network Agent | `COSMOS_ENDPOINT` | Azure Cosmos DB endpoint (needed for change record reads/writes) |
| Network Agent | `COSMOS_DATABASE` | Cosmos DB database name |

**ARCHITECTURE.md updates required:** The Environment Configuration section for the Network Agent must be updated to add `COSMOS_ENDPOINT` and `COSMOS_DATABASE`. The Component Reference for the Network Agent and Agent Gateway must be updated to reflect write capability and the new change management endpoints.

**Note on Coordinator CI/CD:** `services/coordinator/**` is already covered by `deploy-coordinator.yml` via its path trigger. No new workflow file is needed for the Coordinator — changes to `tools/change_reviewer.py` and `main.py` will trigger the existing workflow automatically.

---

## Out of Scope

- Scheduled / pre-approved change windows
- Multi-approver workflows (single human approval only)
- Partial rollback (full config replace only)
- Change templates / playbooks
- Dry-run syntax validation before propose
- Mid-stream cancellation of an in-progress apply
