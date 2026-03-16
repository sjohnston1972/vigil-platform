# VIGIL Platform Architecture

**Visibility and Intelligence for Governed Infrastructure and Lifecycle**

A managed AI operations platform built on Azure AI Foundry, designed for enterprise network and security managed services. VIGIL provides AI-powered network auditing, vulnerability enrichment, ITSM integration, and conversational intelligence over client infrastructure — deployable as a Sword Group managed service.

---

## Table of Contents

1. [Platform Overview](#platform-overview)
2. [Architecture Principles](#architecture-principles)
3. [Component Reference](#component-reference)
4. [Data Flow](#data-flow)
5. [Security Model](#security-model)
6. [Multi-Tenancy](#multi-tenancy)
7. [CI/CD Pipeline](#cicd-pipeline)
8. [Cost Controls](#cost-controls)
9. [Repository Structure](#repository-structure)
10. [Environment Configuration](#environment-configuration)
11. [Technology Decisions](#technology-decisions)

---

## Platform Overview

VIGIL is a multi-tenant, AI-powered managed services platform. It exposes a conversational interface backed by a coordinator agent that orchestrates specialist agents across network, security, ITSM, and knowledge domains.

### Layer Summary

| Layer | Components | Type |
|---|---|---|
| Edge | Cloudflare (WAF, DDoS, custom domain) | Cloudflare |
| Identity | Active Directory DC, Cisco ISE, Cisco Duo | Azure VM, Azure VM, SaaS |
| Client | React UI | Containerised |
| Orchestration | Agent Gateway, Coordinator Agent | Containerised |
| Specialist Agents | Network, RAG, ITSM, Enrichment | Containerised |
| AI | Azure AI Foundry (Claude Sonnet 4.6) | Azure Native |
| Data | Cosmos DB, Azure AI Search, Key Vault | Azure Native |
| Platform | Container Apps, Container Registry | Azure Native |
| CI/CD | GitHub Actions, Jira | GitHub, External SaaS |
| Visibility | Azure Monitor, App Insights, Cost Management | Azure Native |

---

## Architecture Principles

**1. Coordinator pattern — never monolithic**
No single agent does everything. The coordinator delegates to specialists. This mirrors how a managed services team operates — different engineers have different access levels and responsibilities.

**2. Containerised IP, Azure-managed infrastructure**
All VIGIL logic lives in containers. Azure native services handle storage, search, monitoring, and AI. This means the platform is portable — containers move, Azure services get swapped.

**3. Zero trust by default**
Every request is authenticated before it reaches any agent. No direct access to specialist agents from outside the platform. The identity stack — Active Directory, Cisco ISE, and Cisco Duo — handles user authentication and MFA. Azure Managed Identity handles service-to-service auth. Cloudflare handles edge protection and origin locking. No single component owns the full security story.

**4. Multi-tenancy at the gateway**
Tenant isolation is enforced at the Agent Gateway before any downstream processing. Each tenant's conversations, audit logs, and token budgets are completely isolated.

**5. Audit everything**
Every agent action, tool call, and user request is logged to Cosmos DB with tenant ID, user identity, timestamp, and outcome. Non-negotiable for a managed services context.

**6. Fail gracefully**
If a specialist agent fails, the coordinator returns partial results with a clear indication of what failed. The platform never crashes silently.

---

## Component Reference

### Active Directory Domain Controller
- **Type:** Azure VM (Windows Server 2022)
- **Purpose:** Central identity store for VIGIL users and service accounts
- **Responsibilities:**
  - User account management — create, disable, group membership
  - Group Policy for security baseline enforcement
  - DNS for the VIGIL domain
  - LDAP directory services for ISE integration
- **Multi-tenancy note:** Single AD for the showcase. In production, per-tenant AD or trusted forest model depending on client isolation requirements
- **VM sizing:** Standard_B2ms (2 vCPU, 8GB RAM) — sufficient for showcase

### Cisco ISE (Identity Services Engine)
- **Type:** Azure VM (Cisco ISE Marketplace image)
- **Responsibilities:**
  - SAML 2.0 Identity Provider for VIGIL React UI — issues tokens after AD authentication + Duo MFA
  - TACACS+ authentication and authorisation for network device access
  - Policy enforcement — which users can access which resources
  - Device administration — controls what commands the Network Agent can run on devices
- **AD integration:** ISE joins AD domain, uses AD as identity store for user lookups
- **Duo integration:** ISE calls Duo for MFA during authentication flows
- **Multi-tenancy note:** ISE policy sets are scoped per tenant — tenant A's device access policies cannot affect tenant B's devices
- **VM sizing:** Standard_D4s_v3 (4 vCPU, 16GB RAM) — ISE minimum requirement

### Cisco Duo
- **Type:** SaaS (Duo Cloud) with Auth Proxy on Azure VM
- **Responsibilities:**
  - Universal Prompt — web-based MFA challenge during login
  - Second factor enforcement — push notification, TOTP, hardware token
  - MFA bypass policies for service accounts
  - Authentication logs and reporting
- **Integration point:** ISE calls Duo Auth API during SAML authentication flow
- **Auth Proxy:** Small Azure VM running Duo Auth Proxy for RADIUS/LDAP integration with ISE

### Agent Gateway
- **Type:** Containerised (FastAPI)
- **Service:** `services/gateway`
- **Responsibilities:**
  - Validate ISE-issued SAML tokens / JWT on every request
  - Identify and tag requests with tenant ID (extracted from token claims)
  - Enforce per-tenant rate limits (requests per minute)
  - Enforce per-tenant token budgets (daily/monthly) — pre-flight check before proxying
  - Proxy SSE streams from the Coordinator to the UI — non-buffered, using `httpx.AsyncClient` with `aiter_bytes()`
  - Scan the `done` event in the proxied stream to capture `tokens_used` for budget deduction
  - Write audit log entries to Cosmos DB
  - Return structured errors for rate limit, auth, and budget violations
- **Does not:** Execute agent logic, call LLMs, or access network devices
- **SSE proxy pattern:** Returns `StreamingResponse` wrapping an async generator over the `httpx` byte iterator — never buffers the full response

### Coordinator Agent
- **Type:** Containerised (FastAPI + Claude Sonnet 4.6)
- **Service:** `services/coordinator`
- **Responsibilities:**
  - Receive validated requests from the Gateway
  - Load conversation history from Cosmos DB using session ID
  - Load tenant config from Cosmos DB — `max_tokens` limits applied to every Claude call
  - Determine which specialist agents to invoke and in what order
  - Execute multi-step agent workflows via tool calls
  - **Parallel execution:** When Claude returns multiple tool calls in a single response, fan them out concurrently with `asyncio.as_completed()` — each agent completion emits an SSE event immediately as it finishes
  - Handle specialist agent failures gracefully — emit `agent_error` and continue with partial results
  - Stream the final response token by token via SSE
  - Write updated conversation and audit log to Cosmos DB before emitting `done`
- **Endpoints:**
  - `POST /chat/stream` — primary, returns `text/event-stream`
  - `POST /chat` — non-streaming fallback
  - `POST /changes/{change_id}/apply` — phase 2 approval
  - `POST /changes/{change_id}/reject` — reject proposed change
  - `POST /changes/{change_id}/acknowledge-drift` — acknowledge config drift and resume apply
  - `POST /changes/{change_id}/abort` — abort in-flight change
- **Model:** Claude Sonnet 4.6 via Azure AI Foundry
- **Tool definitions:** One tool registered per specialist agent

### Network Agent
- **Type:** Containerised (FastAPI + Netmiko)
- **Service:** `services/agent-network`
- **Responsibilities:**
  - Authenticate to network devices via ISE TACACS+ — no direct credential storage
  - Connect to network devices via SSH using Netmiko
  - **Read operations:** Pull running configurations, interface status, routing tables, ACLs
  - **Write operations:** Propose config/state changes (diff only, nothing applied), apply approved changes, rollback to pre-change config
  - All write operations require `write_enabled: true` in tenant config
  - Pre-change config captured to Cosmos DB before any commands sent — enables safe rollback
  - Background task recovers change records stuck in `applying` state using `applying_started_at` timestamp
  - ISE enforces which devices the agent can access and which commands it can run
- **TACACS+ profiles:** Read profile (`show *` only) and Write profile (explicit permit list + catch-all deny) — write profile per-tenant
- **Supported platforms:** Cisco IOS, IOS-XE, NX-OS, ASA, Palo Alto PAN-OS
- **Never called directly by users** — coordinator only

### RAG Agent
- **Type:** Containerised (FastAPI)
- **Service:** `services/agent-rag`
- **Responsibilities:**
  - Query Azure AI Search knowledge base
  - Return grounded answers with source references
  - Used for compliance checks, best practice lookups, policy validation
- **Knowledge base:** Public domain network and security documentation indexed in Azure AI Search
- **Never called directly by users** — coordinator only

### Change Reviewer Agent
- **Type:** Containerised (FastAPI + Claude Sonnet 4.6)
- **Service:** `services/agent-change-reviewer`
- **Responsibilities:**
  - Receive a proposed network change, device type, and current running config
  - Perform AI peer review assessing correctness, risk/blast radius, and alternatives
  - Return a structured recommendation: `approve`, `flag`, or `reject`
  - Update the `change_records` Cosmos DB document with the review result
  - Never logs or stores `current_config` or `proposed_change` — used only during the Claude review call
- **Always called after `propose_change`** and before the change is presented to the user
- **Never called directly by users** — coordinator only

### ITSM Agent
- **Type:** Containerised (FastAPI)
- **Service:** `services/agent-itsm`
- **Responsibilities:**
  - Create Jira tickets from audit findings
  - Query existing tickets by ID or filter
  - Update ticket status
  - Automatically raise change tickets on platform deployments (triggered by CI/CD)
- **Integration:** Jira REST API (Free tier)
- **Never called directly by users** — coordinator only

### Enrichment Agent
- **Type:** Containerised (FastAPI)
- **Service:** `services/agent-enrichment`
- **Responsibilities:**
  - Look up CVE details from NVD API
  - Check Cisco EoX lifecycle status for device hardware and software
  - Optionally query Shodan for external exposure data
  - Return structured enrichment data for audit findings
- **Never called directly by users** — coordinator only

### React UI
- **Type:** Containerised (React + Vite)
- **Service:** `services/ui`
- **Responsibilities:**
  - Conversational chat interface
  - Admin dashboard — tenant management, token budgets, agent status
  - Audit log viewer
  - Cost monitoring tab
- **Auth:** Redirects unauthenticated users to ISE/Duo login flow via SAML — no auth logic in the application itself

---

## Data Flow

### Standard conversational request (SSE streaming)

```
User hits vigil.{domain}.com
        ↓
Cloudflare (WAF, DDoS protection, origin locking)
        ↓
React UI (unauthenticated — redirects to SAML login)
        ↓
Cisco ISE (SAML IdP — initiates auth flow)
        ↓
Active Directory (validates username/password)
        ↓
Cisco Duo (Universal Prompt — MFA challenge)
        ↓
ISE issues SAML token → React UI
        ↓
Agent Gateway (token validation, tenant ID extraction, rate limit, token budget)
        ↓
Coordinator Agent (loads conversation + tenant config from Cosmos DB)
        ↓ SSE: session_start
[Specialist agents — parallel where Claude returns multiple tool calls]
        ↓ SSE: agent_start (per agent, in immediate succession)
        ↓ SSE: agent_complete / agent_error (as each finishes, real-time)
Azure AI Foundry (Claude Sonnet 4.6 — final response, streaming)
        ↓ SSE: token (per token)
Coordinator writes conversation + audit log → Cosmos DB
        ↓ SSE: done (tokens_used, session_id)
React UI → User
```

### Network device access flow

```
Coordinator → Network Agent
                  ↓
              ISE TACACS+ (authenticates agent service account,
                           checks device access policy,
                           authorises permitted commands)
                  ↓
              Network device (SSH via Netmiko)
                  ↓
              ISE logs all command authorisations
```

### Network change workflow (two-phase)

```
User: "Shut down interface Gi0/1 on 10.0.0.1 — it's showing errors"

Phase 1 — Propose and Review (no device changes):
  Coordinator → Network Agent (propose_change)
    → connects to device (read profile), captures config, generates diff
    → writes change_records document (status: pending)
    SSE: change_proposed (change_id, diff)

  Coordinator → Change Reviewer Agent
    → Claude peer review: correctness, risk, alternatives
    → updates change_records (status: reviewed)
    SSE: change_reviewed (recommendation: flag, risk: "hosts lose connectivity")

  User sees approval modal with diff + peer review
  User clicks "Approve & Apply"

Phase 2 — Apply (after explicit approval):
  Coordinator sets change_records status → approved, approved_by from SAML claims
  Coordinator → Network Agent (apply_change)
    → validates status == approved, not expired
    → checks for config drift → if found: SSE: change_drift_detected, status → drift_pending
    → pushes change_commands to device (write profile)
    → verifies applied
    → updates change_records (status: applied)
    SSE: change_applied (jira_ticket: VIGIL-124)

  Coordinator → ITSM Agent → raises Jira change ticket
```

### Multi-agent audit workflow (parallel execution)

```
User: "Audit the perimeter firewall and raise a ticket for critical findings"

Coordinator:
  Claude returns tool_use: [network_agent, enrichment_agent]  ← parallel
    SSE: agent_start (network_agent, detail: "10.0.0.1")
    SSE: agent_start (enrichment_agent, detail: null)
    asyncio.as_completed() fans both out concurrently
    SSE: agent_complete (enrichment_agent, 890ms)  ← finished first
    SSE: agent_complete (network_agent, 1240ms)    ← finished second

  Claude returns tool_use: [rag_agent, itsm_agent]  ← parallel
    SSE: agent_start (rag_agent)
    SSE: agent_start (itsm_agent)
    asyncio.as_completed() fans both out concurrently
    SSE: agent_complete (rag_agent, 620ms)
    SSE: agent_complete (itsm_agent, 1100ms)

  Claude streams final response → SSE: token (per token)
  Cosmos DB write → SSE: done
```

---

## SSE Streaming Design

### Overview

All chat responses use Server-Sent Events. The Coordinator owns the SSE stream; the Gateway proxies it non-buffered to the UI. The UI uses `fetch` + `ReadableStream` (not `EventSource`, which is GET-only).

### Event flow

```
Coordinator                     Gateway (proxy)                  React UI
    |                               |                               |
    |── session_start ─────────────>|── session_start ────────────>|
    |── agent_start ───────────────>|── agent_start ──────────────>|  (all parallel agents)
    |── agent_start ───────────────>|── agent_start ──────────────>|
    |── agent_complete ────────────>|── agent_complete ───────────>|  (as each finishes)
    |── agent_complete ────────────>|── agent_complete ───────────>|
    |── token ──────────────────── >|── token ────────────────────>|  (per token)
    |── [Cosmos DB write] ─────────|                               |
    |── done ───────────────────── >|── done (budget deduction) ──>|
```

### SSE event schema

| Event | Fields | Notes |
|---|---|---|
| `session_start` | `session_id`, `tenant_id` | First event on every stream |
| `agent_start` | `agent`, `detail: string\|null` | `detail` is agent-defined context (e.g. device host); emitted before execution begins |
| `agent_complete` | `agent`, `duration_ms` | Fires immediately as each agent finishes — not batched |
| `agent_error` | `agent`, `error` | Non-fatal — coordinator continues with partial results |
| `token` | `content` | One token of Claude's streamed final response |
| `done` | `tokens_used`, `session_id` | Emitted after Cosmos DB write — guarantees audit integrity |
| `error` | `code`, `message` | Fatal: `budget_exceeded`, `rate_limited`, `coordinator_unavailable` |

### Cosmos DB write ordering

The Coordinator writes the conversation and audit log to Cosmos DB **before** emitting `done`. If the write fails, `done` is never sent and the UI surfaces an error. This guarantees every completed interaction has a corresponding audit log entry.

### Gateway budget accounting

The Gateway scans the proxied byte stream for the `done` event to extract `tokens_used` and deduct from the tenant budget in Cosmos DB. If the stream ends without a `done` event (client disconnect, upstream failure), the Coordinator's Cosmos DB entry is the authoritative record — the Gateway logs an incomplete session without attempting a budget deduction.

---

## Security Model

### Identity stack

VIGIL uses a layered enterprise identity stack. Each layer has a distinct responsibility:

| Layer | Component | Responsibility |
|---|---|---|
| Edge | Cloudflare | WAF, DDoS, custom domain, origin locking |
| MFA | Cisco Duo | Universal Prompt — second factor enforcement |
| Identity store | Active Directory | User accounts, groups, service accounts |
| Policy enforcement | Cisco ISE | SAML IdP, TACACS+ device access control |
| Token validation | Agent Gateway | Validates ISE tokens, extracts tenant ID |
| Service auth | Azure Managed Identity | All service-to-service Azure calls |

### User authentication flow

Users authenticate via SAML 2.0. ISE acts as the Identity Provider, AD provides the identity store, and Duo enforces MFA. The resulting SAML token contains user identity and tenant claims which the Agent Gateway validates on every request.

### Network device access control

The Network Agent never authenticates directly to devices using stored credentials. All device authentication flows through ISE TACACS+:

- ISE authenticates the Network Agent service account against AD
- ISE checks the device access policy for that tenant
- ISE authorises only the commands permitted by policy
- ISE logs every authentication and command authorisation

This means device access is governed, audited, and revocable from a single point — exactly as a human engineer's access would be.

### Service-to-service authentication
All Azure services authenticate via Azure Managed Identity — no API keys or connection strings in environment variables or code. The Container Apps environment has system-assigned managed identity with RBAC roles scoped to:
- Cosmos DB: Data Contributor
- Azure AI Search: Search Index Data Contributor
- Key Vault: Secrets User
- Azure AI Foundry: Cognitive Services OpenAI User
- Azure Container Registry: AcrPull

### Origin protection
Azure Container Apps origin is locked to Cloudflare IP ranges only. Direct access to the Azure URL is blocked. Cloudflare is the only legitimate ingress path.

### Custom domains
Cloudflare DNS manages all VIGIL custom domains. Proxied through Cloudflare (orange cloud) for WAF, DDoS protection, and automatic SSL.

```
vigil.{client-domain}.com         ← client-facing UI
api.vigil.{client-domain}.com     ← API endpoint (if exposed)
```

### Audit logging
Every request is logged to Cosmos DB with:
- Timestamp
- Tenant ID
- User identity (from ISE SAML token claims)
- Request type
- Agents invoked
- Token consumption
- Outcome (success / error / rate limited / budget exceeded)

ISE maintains its own TACACS+ audit log of all device access independently.

---

## Multi-Tenancy

Each tenant in VIGIL is isolated at every layer:

| Layer | Isolation mechanism |
|---|---|
| Authentication | ISE policy sets scoped per tenant — separate SAML SP per tenant |
| MFA | Duo policies configurable per tenant group in AD |
| AD | Single domain for showcase — OU per tenant, separate forest for production |
| Device access | ISE TACACS+ policy sets scoped per tenant — tenant A cannot access tenant B devices |
| Gateway | Tenant ID extracted from SAML token claims, tagged on all downstream calls |
| Conversation history | Cosmos DB partitioned by tenant ID |
| Audit logs | Cosmos DB partitioned by tenant ID |
| Change records | Cosmos DB partitioned by tenant ID — change_id lookups always use (change_id, tenant_id) |
| Token budgets | Per-tenant daily/monthly limits enforced at Gateway |
| Write capability | `write_enabled` flag per tenant in tenant_config — controls Network Agent write profile activation |
| RAG knowledge base | Azure AI Search index filtered by tenant ID |
| Cost tracking | Azure tags per tenant for cost allocation |

---

## CI/CD Pipeline

### Trigger
Every push to the `main` branch triggers the GitHub Actions pipeline.

### Pipeline steps
1. Checkout code
2. Log in to Azure via service principal
3. Log in to Azure Container Registry
4. Build Docker image tagged with Git SHA
5. Push image to ACR
6. Update Azure Container App to new image revision
7. Raise Jira change ticket with deployment details (production deployments only)

### Branch strategy
- `main` — production, protected branch, requires PR + review
- `dev` — integration branch, deploys to staging environment
- `feature/*` — individual feature branches, no automatic deployment

### Change management
On every merge to `main`, the ITSM Agent raises a Jira change ticket containing:
- Deploying service name
- Git SHA and commit message
- Timestamp
- Deployed by (GitHub Actions actor)

---

## Cost Controls

### Token budgets
Per-tenant daily and monthly token limits are enforced at the Agent Gateway before requests reach the Coordinator. Budget configuration is stored in Cosmos DB and editable via the admin UI.

### Azure Cost Management
Budget alerts are configured at the resource group level (`rg-vigil-prod`). Alerts fire at 80% and 100% of monthly budget. Costs are tagged per tenant for client billing visibility.

### Model selection
Claude Sonnet 4.6 is the default model. Haiku 4.5 is available as a lower-cost option for high-volume, simple queries. Model selection is configurable per tenant.

### Response token limits
`max_tokens` is set per request type — conversational responses are capped lower than audit reports to control costs.

---

## Repository Structure

```
vigil-platform/
├── ARCHITECTURE.md
├── CLAUDE.md
├── README.md
├── .github/
│   └── workflows/
│       ├── deploy-gateway.yml
│       ├── deploy-coordinator.yml
│       ├── deploy-agents.yml
│       └── deploy-ui.yml
├── services/
│   ├── gateway/
│   │   ├── main.py
│   │   ├── middleware/
│   │   │   ├── auth.py          ← ISE SAML token validation
│   │   │   ├── rate_limit.py
│   │   │   └── token_budget.py
│   │   ├── requirements.txt
│   │   ├── Dockerfile
│   │   └── CLAUDE.md
│   ├── coordinator/
│   │   ├── main.py
│   │   ├── tools/
│   │   │   ├── network.py
│   │   │   ├── rag.py
│   │   │   ├── itsm.py
│   │   │   └── enrichment.py
│   │   ├── requirements.txt
│   │   ├── Dockerfile
│   │   └── CLAUDE.md
│   ├── agent-network/
│   │   ├── main.py
│   │   ├── connectors/
│   │   │   ├── cisco_ios.py
│   │   │   ├── cisco_nxos.py
│   │   │   └── palo_alto.py
│   │   ├── tacacs/
│   │   │   └── ise_auth.py      ← ISE TACACS+ auth handler
│   │   ├── requirements.txt
│   │   └── Dockerfile
│   ├── agent-rag/
│   ├── agent-itsm/
│   ├── agent-enrichment/
│   └── ui/
├── infrastructure/
│   └── terraform/
│       ├── main.tf
│       ├── variables.tf
│       ├── outputs.tf
│       ├── backend.tf
│       ├── modules/
│       │   ├── container-apps/
│       │   ├── cosmos-db/
│       │   ├── ai-search/
│       │   ├── key-vault/
│       │   ├── active-directory/ ← Windows Server DC VM
│       │   ├── cisco-ise/        ← ISE VM from Marketplace
│       │   └── duo-proxy/        ← Duo Auth Proxy VM
│       └── environments/
│           ├── dev.tfvars
│           └── prod.tfvars
├── identity/
│   ├── ise/
│   │   ├── policies/            ← ISE policy export/docs
│   │   └── tacacs-profiles/     ← TACACS+ command sets per tenant
│   ├── ad/
│   │   └── ou-structure.md      ← AD OU design per tenant
│   └── duo/
│       └── policy-config.md     ← Duo policy documentation
└── docs/
    ├── onboarding.md
    ├── tenant-setup.md
    ├── identity-setup.md        ← AD + ISE + Duo setup guide
    └── agent-development.md
```

---

## Environment Configuration

### Required environment variables per service

**Gateway**
```
COSMOS_ENDPOINT         # Azure Cosmos DB endpoint
COSMOS_DATABASE         # Database name
ISE_SAML_METADATA_URL   # ISE SAML metadata endpoint for token validation
ISE_SAML_AUDIENCE       # Expected audience in SAML token
```

**Network Agent**
```
ISE_TACACS_HOST         # ISE TACACS+ server IP
ISE_TACACS_KEY          # TACACS+ shared secret (fetched from Key Vault)
COSMOS_ENDPOINT         # Azure Cosmos DB endpoint (for change_records reads/writes)
COSMOS_DATABASE         # Database name
```

**Coordinator**
```
AZURE_FOUNDRY_ENDPOINT       # Azure AI Foundry endpoint
AZURE_FOUNDRY_MODEL          # Model deployment name (claude-sonnet-4-6)
NETWORK_AGENT_URL            # Internal URL of Network Agent
RAG_AGENT_URL                # Internal URL of RAG Agent
ITSM_AGENT_URL               # Internal URL of ITSM Agent
ENRICHMENT_AGENT_URL         # Internal URL of Enrichment Agent
CHANGE_REVIEWER_AGENT_URL    # Internal URL of Change Reviewer Agent
COSMOS_ENDPOINT              # Azure Cosmos DB endpoint
```

**All agents use Azure Managed Identity** — no API keys required for Azure services.

### Secrets (Azure Key Vault)
```
jira-api-token          # Jira API token
jira-base-url           # Jira instance URL
shodan-api-key          # Shodan API key (optional)
tenant-{id}-device-creds # Per-tenant device credentials (JSON)
```

---

## Technology Decisions

| Decision | Choice | Rationale |
|---|---|---|
| LLM | Claude Sonnet 4.6 via Azure Foundry | Billed via Azure MACC, Entra ID auth, best-in-class reasoning |
| Agent framework | Custom FastAPI + Claude tool use | Full control, no framework lock-in, matches existing Gladius patterns |
| Database | Cosmos DB | Managed NoSQL, native Azure, scales per-tenant, serverless billing |
| Vector search | Azure AI Search | Managed RAG, integrates natively with Foundry, no separate vector DB |
| Container runtime | Azure Container Apps | Managed k8s abstraction, built-in ingress, revision management |
| MFA | Cisco Duo Universal Prompt | Enterprise standard, web-based, integrates with ISE natively |
| Identity store | Active Directory (Windows Server VM) | Enterprise standard, required for ISE integration, full control |
| Identity policy | Cisco ISE | SAML IdP + TACACS+ device access, enterprise-grade, Sword expertise |
| Edge protection | Cloudflare | WAF, DDoS, custom domain, origin locking — no auth responsibility |
| CI/CD | GitHub Actions | Team familiarity, native ACR integration, free for private repos |
| Change management | Jira Free tier | REST API available, sufficient for showcase, upgradeable |
| IaC | Terraform | Industry standard, multi-cloud portability, high CV/market value |
| Dev environment | Claude Code + CLAUDE.md | Consistent AI-assisted development across the team |
