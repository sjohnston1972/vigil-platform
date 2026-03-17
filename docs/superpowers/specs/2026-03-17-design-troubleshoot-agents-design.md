# Design & Troubleshooting Agents — Design Spec

**Date:** 2026-03-17
**Status:** Approved
**Scope:** Two new specialist agents — `agent-design` and `agent-troubleshoot` — plus a supporting `agent-probe` execution container.

---

## Overview

Two new agents extend the VIGIL specialist layer:

1. **Design Agent (`agent-design`)** — A RAG-enabled network design consultant covering campus LAN, wireless, Meraki, WAN/SDWAN, security/firewalls, SIP, and telephony. Produces narrative recommendations and structured design artefacts. Never touches live devices. Optionally backed by a Claude-to-Claude critique loop with configurable harshness (UI slider, 1–8).

2. **Troubleshooting Agent (`agent-troubleshoot`)** — An active diagnostic agent with access to tools (dig, curl, nmap, scapy, SSH, vendor firewall APIs). Dispatches execution to a privileged `agent-probe` container. Enforces a three-tier approval model (show / invasive / config) using the existing step-up framework.

Both agents are registered as tools in the coordinator and follow all existing VIGIL patterns: tenant isolation, Cosmos DB with partition key, SSE event emission, structured Pydantic responses, and `/health` endpoints.

**Note on secret fetching:** Per CLAUDE.md, all platform-wide secrets are fetched at startup. Per-tenant vendor credentials are an approved exception — they are fetched just-in-time per request (same pattern as step-up write credentials).

---

## Architecture

```
Coordinator
  ├── design_agent ──────────────► agent-design (FastAPI + Claude loop)
  │                                    ├── RAG Agent (existing, called internally)
  │                                    └── Critique sub-agent (Claude call, internal)
  │
  ├── troubleshoot_agent ─────────► agent-troubleshoot (FastAPI, unprivileged)
  │                                    ├── agent-probe (privileged container)
  │                                    └── vendor-connectors/ (extensible plugin registry)
  │
  └── [existing agents unchanged]
```

### New Cosmos DB containers

| Container | Partition key | TTL | Purpose |
|---|---|---|---|
| `design_sessions` | `tenant_id` | None | Design artefacts + critique iteration history |

`step_up_requests`, `step_up_grants`, and `audit_logs` reused unchanged for Troubleshoot Agent approval gates.

### New coordinator env vars

```
DESIGN_AGENT_URL
TROUBLESHOOT_AGENT_URL
```

### New troubleshoot agent env vars

```
PROBE_URL              # internal URL of agent-probe
PROBE_TIMEOUT_SECONDS  # default: 20 — probe kills process and returns error after this duration
```

---

## Design Agent

### Service

- **Path:** `services/agent-design/`
- **Container App:** `vigil-agent-design`
- **Capabilities:** None (standard unprivileged)
- **Internal only:** Yes

### RAG Integration

Calls the existing RAG Agent internally before generating a design, grounding output in the indexed knowledge base. Domain context (`campus_lan`, `wireless`, `meraki`, `wan_sdwan`, `security_firewall`, `sip_telephony`) is passed in the RAG query to scope retrieval. If `domain_hints` is omitted, the Design Agent infers domains from the query text before calling the RAG Agent.

Emits `design_rag_start` before the RAG call and `design_rag_complete` after, following the `_start` / `_complete` SSE convention.

### Design Generation

Single Claude Sonnet 4.6 call with a system prompt establishing it as a senior network architect. Input: user query + RAG-retrieved context + domain hints. Output: structured JSON:

```python
{
    "narrative": str,            # prose HLD/LLD recommendations
    "artefacts": list[dict],     # topology descriptions, config templates, IP schemes, VLAN tables, etc.
    "domains_covered": list[str],
    "assumptions": list[str],
    "open_questions": list[str]
}
```

### Critique Loop (optional, UI-triggered)

**Loop is request-stateless.** After each critique iteration, the Design Agent writes the iteration result to `design_sessions` and terminates the SSE stream. The UI re-submits the request with the same `design_session_id` to continue, or calls `POST /design/{design_session_id}/accept` to terminate. This approach survives container restarts and requires no in-memory session state.

When `critique_enabled=True`, the agent checks `design_session_id` on arrival:
- If absent or no prior iterations: runs design generation (iteration 1), then critique.
- If present: loads prior iterations from `design_sessions`, runs next iteration of design + critique.

Per-iteration flow (up to 5 iterations):

1. Design Claude call (informed by prior critique if iteration > 1) → draft
2. Critique Claude call → structured critique result: `{ score: int (1–10), issues: list[dict], verdict: "accept"|"revise" }`
3. Write iteration to `design_sessions`
4. Emit `critique_iteration` SSE
5. Terminate stream — UI surfaces the iteration card to the user
6. User clicks **Accept** → calls accept endpoint → loop terminates
7. User clicks **Continue** → re-submits with `design_session_id` → next iteration

If `verdict == "accept"` (Critique deems design sufficient) or `iterations == 5` (max reached), the agent writes `final_artefacts` and emits `critique_complete` before terminating.

**Critique score range: 1–10.** A score of 10 means no issues found; a score below 5 triggers automatic `verdict: "revise"` regardless of the listed issues.

**Slider → system prompt mapping (`critique_level` 1–8):**

| Level | Critique posture |
|---|---|
| 1 | Identify only critical errors |
| 2–3 | Flag significant risks and gaps |
| 4–5 | Challenge design choices, suggest alternatives |
| 6–7 | Demand justification for all decisions, stress-test assumptions |
| 8 | Adversarial — challenge everything, require alternatives for every approach |

### Accept endpoint

`POST /design/{design_session_id}/accept`

Called via the Gateway when the user clicks "Accept design as-is". Uses the standard platform pattern — `tenant_id` is passed in the Pydantic request body, populated by the Gateway from the validated SAML token claims before proxying to the Design Agent.

Request model:
```python
class DesignAcceptRequest(BaseModel):
    tenant_id: str
```

The agent must:
1. Read the `design_sessions` document: `container.read_item(item=design_session_id, partition_key=tenant_id)`.
2. Verify `record["tenant_id"] == tenant_id` — reject with 403 if mismatched.
3. Write `final_artefacts` from the most recent iteration, set `accepted_at`, return 200.

### Tool input schema

```python
{
    "query":             str,         # required — user's design question
    "domain_hints":      list[str],   # optional — e.g. ["wan_sdwan", "security_firewall"]; inferred from query if omitted
    "critique_enabled":  bool,        # required
    "critique_level":    int,         # required — 1–8
    "design_session_id": str | None   # optional — for continuing a prior critique session
}
```

### SSE events

| Event | Fields |
|---|---|
| `design_rag_start` | `domains` |
| `design_rag_complete` | `domains`, `chunks_retrieved` |
| `design_draft_ready` | `iteration_n`, `design_session_id` |
| `critique_iteration` | `iteration_n`, `score` (1–10), `issues`, `verdict`, `design_session_id` |
| `critique_complete` | `final_score`, `iterations_taken`, `design_session_id` |
| `done` | `tokens_used`, `session_id` — emitted after Cosmos DB write on all termination paths |

### Cosmos DB document (`design_sessions`)

Always use `container.read_item(item=design_session_id, partition_key=tenant_id)`. Never cross-partition query. The container partition key is `tenant_id`; the item `id` is `design_session_id`.

```python
{
    "id": design_session_id,            # uuid
    "tenant_id": tenant_id,
    "session_id": session_id,           # links back to coordinator conversation
    "query": str,
    "domain_hints": list[str],
    "critique_enabled": bool,
    "critique_level": int,
    "iterations": [
        {
            "n": int,
            "draft": {
                "narrative": str,
                "artefacts": list[dict],
                "domains_covered": list[str],
                "assumptions": list[str],
                "open_questions": list[str]
            },
            "critique": { "score": int, "issues": list[dict], "verdict": str } | None,
            "accepted_at": str | None
        }
    ],
    "final_artefacts": {
        "narrative": str,
        "artefacts": list[dict],
        "domains_covered": list[str],
        "assumptions": list[str],
        "open_questions": list[str]
    } | None,
    "created_at": str,
    "updated_at": str
}
```

---

## Troubleshooting Agent

### Services

| Service | Path | Container App | Capabilities | Internal |
|---|---|---|---|---|
| `agent-troubleshoot` | `services/agent-troubleshoot/` | `vigil-agent-troubleshoot` | None | Yes |
| `agent-probe` | `services/agent-probe/` | `vigil-agent-probe` | `NET_ADMIN`, `NET_RAW` | Yes — VNet-locked to troubleshoot agent only |

**Infrastructure note:** `agent-probe` requires the **Dedicated workload profile** on Azure Container Apps to support `NET_ADMIN` / `NET_RAW` capabilities. The Consumption profile does not support custom Linux capabilities. The Terraform module for `agent-probe` must specify `workload_profile_name = "Dedicated"`.

### Three-Tier Approval Model

| Tier | Examples | Approval |
|---|---|---|
| **Show** | dig, curl, nslookup, passive ping, API reads, firewall `show` commands | None — dispatches immediately |
| **Invasive** | nmap aggressive profiles, scapy packet injection, bulk SSH `show` pulls, traceroute flooding | Warning SSE + step-up approval |
| **Config** | Firewall rule add/edit/delete, interface config, route changes via vendor API | Step-up approval + Jira ticket (skippable with `emergency: true`) |

Tier is determined by the Troubleshoot Agent based on tool name and parameters — not by the user or coordinator.

**Emergency flag enforcement:** The `emergency: true` flag (which skips Jira for config-tier changes) must be verified server-side by the Troubleshoot Agent. The agent reads the `X-User-Claims` header set by the Gateway (containing SAML token claims) and checks for the `emergency_change` role. If the claim is absent, `emergency: true` is rejected with 403 regardless of what the coordinator passed. The UI renders the checkbox only when the claim is present, but this is a UX convenience — the server-side check is authoritative.

**Step-up flow for invasive/config tiers (request-stateless):**

The step-up approval flow does not block the SSE stream indefinitely. After determining a tool requires approval:

1. Agent writes a `step_up_requests` document (same schema as existing step-up framework).
2. Emits `probe_warning` SSE with `request_id`.
3. Terminates the SSE stream.
4. The UI renders the approval modal. The user approves or rejects out-of-band via the existing `POST /step-up/{request_id}/approve` endpoint on the coordinator.
5. On approval, the Gateway/UI re-submits the troubleshoot request with `step_up_grant_id` in the payload.
6. The Troubleshoot Agent validates the grant (checks `step_up_grants` Cosmos DB for an active grant scoped to the tool + tenant), then dispatches to probe.

This mirrors the existing step-up pattern used by `apply_change` and `rollback_change`.

### Probe Container (`agent-probe`)

Pure execution engine — no tenant awareness, no business logic. Accepts connections only from the internal Container Apps VNet. The Troubleshoot Agent is responsible for audit logging of all probe invocations (see Audit Logging below).

**Dispatch model: one probe call per target.** The Troubleshoot Agent loops over `targets` and issues a separate `POST /execute` per target. This keeps the SSE model (`probe_start` / `probe_complete` per target), the audit log schema (singular `target` field), and the probe interface simple and consistent.

**Endpoint:**
```
POST /execute
{
  "tool":             "nmap" | "scapy" | "dig" | "curl" | "ssh",
  "target":           str,
  "params":           dict,
  "timeout_seconds":  int
}
→ { "stdout": str, "stderr": str, "exit_code": int, "parsed": dict | null }
```

All output is returned to the Troubleshoot Agent for parsing into structured findings before returning to the coordinator.

### Audit Logging

The Troubleshoot Agent writes an `audit_logs` entry for every probe invocation (partitioned by `tenant_id`). Minimum fields:

```python
{
    "id": audit_id,
    "tenant_id": tenant_id,
    "session_id": session_id,
    "event_type": "probe_invocation",
    "tool": str,
    "target": str,
    "tier": "show" | "invasive" | "config",
    "step_up_request_id": str | None,
    "emergency": bool,
    "outcome": "success" | "error" | "rejected" | "interrupted",
    "duration_ms": int,
    "tokens_used": int,           # Claude tool-selection tokens; 0 for show-tier (no internal Claude call)
    "budget_deducted": bool,      # false on write; set to true by Gateway reconciliation task
    "timestamp": str
}
```

On non-interrupted streams, `budget_deducted` is set to `true` immediately on write (the `done` event handles deduction via the existing Gateway path). On interrupted streams, `budget_deducted` starts `false` and is updated by the reconciliation task.

### Firewall Connector Registry

Plugin pattern in `agent-troubleshoot/connectors/`:

```
connectors/
  __init__.py      ← registry: { vendor_id: ConnectorClass }
  base.py          ← abstract FirewallConnector with show(), get_config(), push_config()
  palo_alto.py     ← PAN-OS REST API + Panorama
  cisco_asa.py     ← ASA REST API / FTD Firepower
  cisco_meraki.py  ← Meraki Dashboard API
  fortinet.py      ← FortiGate REST API
```

Adding a new vendor = new file + one registry entry. No other changes required.

**Connector tier mapping:** `show()` → show tier (no approval). `get_config()` → show tier for targeted lookups, invasive tier for broad scope pulls (agent determines based on scope). `push_config()` → config tier — the Troubleshoot Agent must verify an active step-up grant before calling `push_config()` on any connector; the connector itself does not enforce this.

**Vendor credentials are fetched just-in-time per request** (approved exception to the startup-fetch rule, same as step-up write credentials). The Troubleshoot Agent fetches `tenant-{tenant_id}-{vendor}-creds` from Key Vault on each request using the `tenant_id` from the incoming SAML token. Credentials are not cached in module scope. This prevents a compromised container from holding all tenants' firewall credentials in memory simultaneously.

### Key Vault secrets (new)

```
tenant-{id}-palo-alto-creds
tenant-{id}-cisco-asa-creds
tenant-{id}-meraki-creds
tenant-{id}-fortinet-creds
```

### Jira ticket (config-tier)

Config-tier operations call the existing `agent-itsm` service (reuses the existing Jira integration — no direct Jira API calls from the Troubleshoot Agent). Minimum ticket fields:

```python
{
    "summary": f"[VIGIL]{'EMERGENCY ' if emergency else ''}Troubleshoot config change — {tool} on {target}",
    "description": f"Tenant: {tenant_id}\nUser: {user_identity}\nTool: {tool}\nTarget: {target}\nParams: {params}\nTimestamp: {timestamp}",
    "project": "VIGIL",
    "issue_type": "Change",
    "priority": "High" if emergency else "Medium"
}
```

Ticket lifecycle:
- Created **after** step-up approval, **before** probe dispatch (or skipped if `emergency=True`).
- If step-up is rejected, no ticket is raised.
- Ticket ID is emitted in the `config_change_jira` SSE event.

### Tool input schema

```python
{
    "task":             str,         # required — natural language troubleshoot request
    "targets":          list[str],   # required — IPs, hostnames, or device identifiers
    "tools_hint":       list[str],   # optional — constrain tool selection e.g. ["nmap", "dig"]
    "emergency":        bool,        # optional, default false — requests Jira skip for config-tier (server-side verified; absent treated as false)
    "vendor":           str | None,  # optional — firewall vendor for API-based tools
    "step_up_grant_id": str | None   # optional — populated on re-submission after approval
}
```

The Troubleshoot Agent uses Claude internally to select tools and execution order based on the task. Selection is constrained to available probe tools and vendor connectors.

### SSE events

| Event | Fields |
|---|---|
| `probe_start` | `tool`, `target`, `tier` |
| `probe_warning` | `tool`, `target`, `tier: "invasive"\|"config"`, `reason`, `request_id` — terminal event for interrupted streams |
| `probe_complete` | `tool`, `target`, `duration_ms`, `summary` |
| `probe_error` | `tool`, `target`, `error` |
| `config_change_jira` | `ticket_id`, `tool`, `target` |
| `done` | `tokens_used`, `session_id` — emitted after Cosmos DB write on successful (non-interrupted) stream termination |

**Stream termination on step-up interrupt:** When a tool requires invasive or config-tier approval, the Troubleshoot Agent emits `probe_warning` as the final event and terminates the stream. No `done` event is emitted on interrupted streams. The Gateway's `log_incomplete_session` path handles this correctly — it is the intended outcome.

**Compensating budget deduction for interrupted streams:** Tokens consumed by the internal Claude tool-selection call on an interrupted path must still be deducted from the tenant budget. The mechanism:

1. The `audit_logs` entry written on interrupt includes two additional fields: `tokens_used: int` and `budget_deducted: false`.
2. The Gateway runs a background reconciliation task (alongside the existing `_run_recovery_loop` pattern in the coordinator) that periodically scans `audit_logs` for entries where `budget_deducted == false` and `tokens_used > 0`, partitioned per tenant.
3. For each such entry, the task deducts `tokens_used` from the tenant budget in Cosmos DB and updates `audit_logs` to `budget_deducted: true`.
4. Reconciliation runs every 5 minutes. Atomic update pattern (optimistic concurrency via ETag) prevents double-deduction on concurrent runs.

This ensures all token consumption — including from interrupted gated operations — is accounted for in tenant budgets, with no changes needed to the SSE or Gateway stream path.

---

## Coordinator Integration

### Tool registrations

**`services/coordinator/tools/design.py`**

```python
{
    "name": "design_agent",
    "description": "Expert network design consultant covering campus LAN, wireless, Meraki, WAN/SDWAN, security, firewalls, SIP, and telephony. Use when the user asks to design, plan, architect, or recommend a network solution. Returns narrative recommendations and structured design artefacts. Never makes changes to live devices.",
    "input_schema": {
        "type": "object",
        "properties": {
            "query":             { "type": "string" },
            "domain_hints":      { "type": "array", "items": { "type": "string" } },
            "critique_enabled":  { "type": "boolean" },
            "critique_level":    { "type": "integer", "minimum": 1, "maximum": 8 },
            "design_session_id": { "type": "string" }
        },
        "required": ["query", "critique_enabled", "critique_level"]
    }
}
```

`domain_hints` is optional — the Design Agent infers domains from the query when omitted.

**`services/coordinator/tools/troubleshoot.py`**

```python
{
    "name": "troubleshoot_agent",
    "description": "Active network troubleshooting using diagnostic tools (dig, curl, nmap, scapy, SSH, vendor firewall APIs). Use when the user asks to diagnose, test connectivity, trace a path, check a firewall rule, or investigate a network fault. Some tools require step-up approval — the agent enforces this automatically based on tool risk tier.",
    "input_schema": {
        "type": "object",
        "properties": {
            "task":             { "type": "string" },
            "targets":          { "type": "array", "items": { "type": "string" } },
            "tools_hint":       { "type": "array", "items": { "type": "string" } },
            "emergency":        { "type": "boolean" },
            "vendor":           { "type": "string" },
            "step_up_grant_id": { "type": "string" }
        },
        "required": ["task", "targets"]
    }
}
```

### `agent_loop.py` changes

Two new entries in `_get_agent_url`:

```python
"design_agent":       os.getenv("DESIGN_AGENT_URL", ""),
"troubleshoot_agent": os.getenv("TROUBLESHOOT_AGENT_URL", ""),
```

- `design_agent` is non-gated — runs in parallel with other non-gated tools.
- `troubleshoot_agent` invasive and config tiers use the existing step-up framework. The step-up document is written by the Troubleshoot Agent (not the coordinator). The coordinator is not modified for approval routing — existing `/step-up/{request_id}/approve` and `/reject` endpoints handle it.

---

## UI Changes

### Design Agent controls

A collapsible **"Design Assistant"** panel in the chat input area, rendered when `design_rag_start` or `design_draft_ready` SSE events are observed (or via a persistent user toggle).

**Critique toggle + slider:**
```
[ Enable Critique ]  ●━━━━━━━━━━━○
  Go easy  |  Balanced  |  Both barrels
```

Slider: 8 positions, labels at 1 / 4 / 8. Value persisted in `localStorage` per tenant.

**Critique iteration card** (rendered on each `critique_iteration` SSE):
```
┌─ Critique Round N/5 — Score: X/10 ─────────────────┐
│ ⚠  <issue description>                              │
│ ✓  <passing area>                                   │
│                                                      │
│  [ Accept design as-is ]  [ Continue refining → ]   │
└──────────────────────────────────────────────────────┘
```

- **Accept** → calls `POST /design/{design_session_id}/accept` via the Gateway (tenant_id from SAML token).
- **Continue** → re-submits the original chat request with `design_session_id` populated.

### Troubleshooting Agent controls

**Invasive warning modal** (rendered on `probe_warning` SSE, before step-up):
```
┌─ ⚠ Invasive Scan Requested ────────────────────────┐
│ Tool: <tool + params>  →  Target: <target>          │
│ This scan generates significant traffic and may      │
│ trigger IDS alerts on the target network.           │
│                                                      │
│  [ Cancel ]   [ Approve & Continue ]                │
└─────────────────────────────────────────────────────┘
```

Approve → calls existing `POST /step-up/{request_id}/approve`. On approval, UI re-submits the troubleshoot request with `step_up_grant_id`.

**Config tier** — uses the existing step-up approval modal with an added **"Emergency — skip Jira"** checkbox. This checkbox is only rendered when the `emergency_change` role claim is present in the user's decoded SAML token. Server-side enforcement (independent of the UI) is described above.

**Probe progress rows** — `probe_start` / `probe_complete` render as rows in the existing agent sidebar (amber → green dot), consistent with `agent_start` / `agent_complete`. The `summary` field renders as a one-line result beneath the tool name.

---

## Infrastructure

### New Container Apps

| Container App | Image | Workload profile | Capabilities |
|---|---|---|---|
| `vigil-agent-design` | `vigil-agent-design` | Consumption | None |
| `vigil-agent-troubleshoot` | `vigil-agent-troubleshoot` | Consumption | None |
| `vigil-agent-probe` | `vigil-agent-probe` | **Dedicated** | `NET_ADMIN`, `NET_RAW` |

`agent-probe` must use the Dedicated workload profile — the Consumption profile does not support custom Linux capabilities.

### Terraform

Three new Container App resources in `infrastructure/terraform/modules/container-apps/`. The probe module specifies `workload_profile_name = "Dedicated"` and a `capabilities` block. The Dedicated profile incurs fixed compute cost regardless of invocation — this is an accepted trade-off for the security isolation it provides.

### GitHub Actions

Three new path-filtered deploy workflows:
- `.github/workflows/deploy-agent-design.yml`
- `.github/workflows/deploy-agent-troubleshoot.yml`
- `.github/workflows/deploy-agent-probe.yml`

---

## ARCHITECTURE.md Updates Required

- Design Agent + Troubleshoot Agent added to Component Reference
- `agent-probe` noted as a privileged supporting execution container (Dedicated workload profile)
- `design_sessions` added to Cosmos DB container list
- New env vars added to Environment Configuration table
- Data flow section: add "Network design workflow" example
- Repository structure diagram updated

---

## Testing Requirements

Per-service minimum (per CLAUDE.md):

**agent-design:**
- `/health` returns 200
- Design endpoint returns correct artefact structure (narrative, artefacts, domains_covered, assumptions, open_questions)
- Critique loop respects `max_iterations=5`
- `critique_level` 1 vs 8 produces measurably different Critique system prompts
- Re-submission with `design_session_id` correctly loads prior iterations and runs next iteration
- Accept endpoint verifies `record["tenant_id"] == tenant_id`; returns 403 on mismatch
- Tenant A cannot read tenant B's `design_sessions` documents

**agent-troubleshoot:**
- `/health` returns 200
- Show-tier tools dispatch without approval
- Invasive-tier tools emit `probe_warning`, write `step_up_requests` record, terminate stream
- Config-tier tools create `step_up_requests` + Jira ticket via `agent-itsm`; `emergency=True` skips Jira
- `emergency=True` without `emergency_change` SAML claim returns 403
- `step_up_grant_id` on re-submission validates grant before probe dispatch
- Audit log entry written for every probe invocation including `tokens_used` and `budget_deducted`
- Interrupted stream audit entry has `budget_deducted: false`; reconciliation task flips it to `true` and deducts from budget
- Tenant isolation on `step_up_requests` reads

**agent-probe:**
- `/health` returns 200
- Each supported tool executes and returns structured output
- Timeout respected — process killed and error returned after `timeout_seconds`

**Infrastructure acceptance test (not pytest):**
- VNet ingress lock verified via `az containerapp show --name vigil-agent-probe | jq '.properties.configuration.ingress.external'` → must return `false`
- Confirmed in Terraform plan output: `ingress.external_enabled = false`
