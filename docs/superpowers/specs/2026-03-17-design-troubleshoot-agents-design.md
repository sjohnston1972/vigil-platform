# Design & Troubleshooting Agents — Design Spec

**Date:** 2026-03-17
**Status:** Approved
**Scope:** Two new specialist agents — `agent-design` and `agent-troubleshoot` — plus a supporting `agent-probe` execution container.

---

## Overview

Two new agents extend the VIGIL specialist layer:

1. **Design Agent (`agent-design`)** — A RAG-enabled network design consultant covering campus LAN, wireless, Meraki, WAN/SDWAN, security/firewalls, SIP, and telephony. Produces narrative recommendations and structured design artefacts. Never touches live devices. Optionally backed by a Claude-to-Claude critique loop with configurable harshness (UI slider, 1–8).

2. **Troubleshooting Agent (`agent-troubleshoot`)** — An active diagnostic agent with access to tools (dig, curl, nmap, scapy, SSH, vendor firewall APIs). Dispatches execution to a privileged `agent-probe` container. Enforces a three-tier approval model (show / invasive / config) using the existing step-up framework.

Both agents are registered as tools in the coordinator and follow all existing VIGIL patterns: tenant isolation, Cosmos DB with partition key, Key Vault secret fetching at startup, SSE event emission, structured Pydantic responses, and `/health` endpoints.

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

### New troubleshoot agent env var

```
PROBE_URL   # internal URL of agent-probe
```

---

## Design Agent

### Service

- **Path:** `services/agent-design/`
- **Container App:** `vigil-agent-design`
- **Capabilities:** None (standard unprivileged)
- **Internal only:** Yes

### RAG Integration

Calls the existing RAG Agent internally before generating a design, grounding output in the indexed knowledge base. Domain context (`campus_lan`, `wireless`, `meraki`, `wan_sdwan`, `security_firewall`, `sip_telephony`) is passed in the RAG query to scope retrieval.

### Design Generation

Single Claude Sonnet 4.6 call with a system prompt establishing it as a senior network architect. Input: user query + RAG-retrieved context + domain hints. Output: structured JSON:

```python
{
    "narrative": str,          # prose HLD/LLD recommendations
    "artefacts": list[dict],   # topology descriptions, config templates, IP schemes, VLAN tables, etc.
    "domains_covered": list[str],
    "assumptions": list[str],
    "open_questions": list[str]
}
```

### Critique Loop (optional, UI-triggered)

When `critique_enabled=True`, the agent runs up to 5 iterations:

1. Design Claude call → draft
2. Critique Claude call → structured critique result: `{ score: int, issues: [...], verdict: "accept"|"revise" }`
3. If `verdict == "revise"` and `iterations < 5`:
   - Emit `critique_iteration` SSE → surface to user
   - User chooses **Accept** (terminates loop) or **Continue** (loop proceeds)
4. If `verdict == "accept"` OR user accepts OR `iterations == 5`: emit `critique_complete`, deliver final design

**Slider → system prompt mapping (`critique_level` 1–8):**

| Level | Critique posture |
|---|---|
| 1 | Identify only critical errors |
| 2–3 | Flag significant risks and gaps |
| 4–5 | Challenge design choices, suggest alternatives |
| 6–7 | Demand justification for all decisions, stress-test assumptions |
| 8 | Adversarial — challenge everything, require alternatives for every approach |

### Accept endpoint

`POST /design/{design_session_id}/accept` — called directly from the UI via the Gateway when the user clicks "Accept design as-is". Writes final artefacts to `design_sessions` and terminates the loop.

### Tool input schema

```python
{
    "query":             str,        # required — user's design question
    "domain_hints":      list[str],  # required — e.g. ["wan_sdwan", "security_firewall"]
    "critique_enabled":  bool,       # required
    "critique_level":    int,        # required — 1–8
    "design_session_id": str | None  # optional — for continuing a prior session
}
```

### SSE events

| Event | Fields |
|---|---|
| `design_rag_start` | `domains` |
| `design_draft_ready` | `iteration_n` |
| `critique_iteration` | `iteration_n`, `score`, `issues`, `verdict` |
| `critique_complete` | `final_score`, `iterations_taken` |

### Cosmos DB document (`design_sessions`)

```python
{
    "id": design_session_id,
    "tenant_id": tenant_id,
    "session_id": session_id,
    "query": str,
    "domain_hints": list[str],
    "critique_enabled": bool,
    "critique_level": int,
    "iterations": [
        {
            "n": int,
            "draft": { "narrative": str, "artefacts": list },
            "critique": { "score": int, "issues": list, "verdict": str } | None,
            "accepted_at": str | None
        }
    ],
    "final_artefacts": { "narrative": str, "artefacts": list },
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

### Three-Tier Approval Model

| Tier | Examples | Approval |
|---|---|---|
| **Show** | dig, curl, nslookup, passive ping, API reads, firewall `show` commands | None — dispatches immediately |
| **Invasive** | nmap aggressive profiles, scapy packet injection, bulk SSH `show` pulls, traceroute flooding | Warning SSE + step-up approval |
| **Config** | Firewall rule add/edit/delete, interface config, route changes via vendor API | Step-up approval + Jira ticket (skippable with `emergency: true`) |

Tier is determined by the Troubleshoot Agent based on tool name and parameters — not by the user or coordinator.

Emergency Jira skip is only available to users with the `emergency_change` role claim in their ISE SAML token. The UI renders the checkbox only when this claim is present.

### Probe Container (`agent-probe`)

Pure execution engine — no tenant awareness, no business logic. Accepts connections only from the internal Container Apps VNet.

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

Vendor credentials fetched from Key Vault at startup using the namespace `tenant-{tenant_id}-{vendor}-creds`.

### Key Vault secrets (new)

```
tenant-{id}-palo-alto-creds
tenant-{id}-cisco-asa-creds
tenant-{id}-meraki-creds
tenant-{id}-fortinet-creds
```

### Tool input schema

```python
{
    "task":        str,         # required — natural language troubleshoot request
    "targets":     list[str],   # required — IPs, hostnames, or device identifiers
    "tools_hint":  list[str],   # optional — constrain tool selection e.g. ["nmap", "dig"]
    "emergency":   bool,        # required — skips Jira for config-tier changes
    "vendor":      str | None   # optional — firewall vendor for API-based tools
}
```

The Troubleshoot Agent uses Claude internally to select tools and execution order based on the task. Selection is constrained to available probe tools and vendor connectors.

### SSE events

| Event | Fields |
|---|---|
| `probe_start` | `tool`, `target`, `tier` |
| `probe_warning` | `tool`, `target`, `tier: "invasive"`, `reason` |
| `probe_complete` | `tool`, `target`, `duration_ms`, `summary` |
| `probe_error` | `tool`, `target`, `error` |
| `config_change_jira` | `ticket_id`, `tool`, `target` |

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

**`services/coordinator/tools/troubleshoot.py`**

```python
{
    "name": "troubleshoot_agent",
    "description": "Active network troubleshooting using diagnostic tools (dig, curl, nmap, scapy, SSH, vendor firewall APIs). Use when the user asks to diagnose, test connectivity, trace a path, check a firewall rule, or investigate a network fault. Some tools require step-up approval — the agent enforces this automatically based on tool risk tier.",
    "input_schema": {
        "type": "object",
        "properties": {
            "task":        { "type": "string" },
            "targets":     { "type": "array", "items": { "type": "string" } },
            "tools_hint":  { "type": "array", "items": { "type": "string" } },
            "emergency":   { "type": "boolean" },
            "vendor":      { "type": "string" }
        },
        "required": ["task", "targets", "emergency"]
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
- `troubleshoot_agent` invasive and config tiers use the existing step-up framework — `step_up_requests` documents written by the Troubleshoot Agent, not the coordinator.

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

Accept calls `POST /design/{design_session_id}/accept` via the Gateway.

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

**Config tier** — uses the existing step-up approval modal with an added **"Emergency — skip Jira"** checkbox, visible only when `emergency_change` role claim is present in the user's ISE SAML token.

**Probe progress rows** — `probe_start` / `probe_complete` render as rows in the existing agent sidebar (amber → green dot), consistent with `agent_start` / `agent_complete`. The `summary` field renders as a one-line result beneath the tool name.

---

## Infrastructure

### New Container Apps

| Container App | Image | Capabilities |
|---|---|---|
| `vigil-agent-design` | `vigil-agent-design` | None |
| `vigil-agent-troubleshoot` | `vigil-agent-troubleshoot` | None |
| `vigil-agent-probe` | `vigil-agent-probe` | `NET_ADMIN`, `NET_RAW` |

### Terraform

Three new Container App resources in `infrastructure/terraform/modules/container-apps/`. Probe module includes a `capabilities` block — only infrastructure difference from other agents.

### GitHub Actions

Three new path-filtered deploy workflows:
- `.github/workflows/deploy-agent-design.yml`
- `.github/workflows/deploy-agent-troubleshoot.yml`
- `.github/workflows/deploy-agent-probe.yml`

---

## ARCHITECTURE.md Updates Required

- Design Agent + Troubleshoot Agent added to Component Reference
- `agent-probe` noted as a privileged supporting execution container
- `design_sessions` added to Cosmos DB container list
- New env vars added to Environment Configuration table
- Data flow section: add "Network design workflow" example
- Repository structure diagram updated

---

## Testing Requirements

Per-service minimum (per CLAUDE.md):

**agent-design:**
- `/health` returns 200
- Design endpoint returns correct artefact structure
- Critique loop respects `max_iterations=5`
- `critique_level` 1 vs 8 produces measurably different system prompts
- Tenant A cannot read tenant B's `design_sessions` documents

**agent-troubleshoot:**
- `/health` returns 200
- Show-tier tools dispatch without approval
- Invasive-tier tools emit `probe_warning` and create `step_up_requests` record
- Config-tier tools create `step_up_requests` + Jira ticket; `emergency=true` skips Jira
- Tenant isolation on `step_up_requests` reads

**agent-probe:**
- `/health` returns 200
- Each supported tool executes and returns structured output
- Timeout respected
- Connections rejected from outside the VNet
