# VIGIL Platform

**Visibility and Intelligence for Governed Infrastructure and Lifecycle**

A multi-tenant, AI-powered managed services platform built on Azure. VIGIL provides AI-driven network auditing, vulnerability enrichment, ITSM integration, and conversational intelligence over client infrastructure — delivered as a Sword Group managed service.

---

## Overview

VIGIL exposes a conversational chat interface backed by a central coordinator agent (Claude Sonnet 4.6) that orchestrates specialist agents across network, security, ITSM, and knowledge domains. All agent activity streams to the user in real time via Server-Sent Events — parallel agent calls are visible as they happen, and the final response streams token by token.

### Key Capabilities

| Capability | Description |
|---|---|
| Network auditing | SSH into Cisco/Palo Alto devices via ISE TACACS+, retrieve config, interfaces, routes, ACLs |
| Network changes | Propose, AI peer review, human approve, apply, and rollback config/state changes — two-phase with full audit trail |
| Step-up auth | Human-in-the-loop approval gates for high-risk tool calls — SSE-streamed, out-of-band email/webhook notifications, time-window grants |
| Vulnerability enrichment | NVD CVE lookup, Cisco EoX lifecycle status, optional Shodan exposure check |
| RAG knowledge base | Azure AI Search over compliance docs, best practices, and policy documents |
| ITSM integration | Create and query Jira tickets from audit findings and every applied network change |
| Real-time streaming | SSE streams agent progress and Claude's response token by token |
| Multi-tenancy | Complete tenant isolation at every layer — auth, data, device access, token budgets |

---

## Architecture

### Platform Layers

```mermaid
graph TB
    subgraph Edge
        CF[Cloudflare<br/>WAF · DDoS · Origin Lock]
    end

    subgraph Identity
        AD[Active Directory<br/>Windows Server 2022]
        ISE[Cisco ISE<br/>SAML IdP · TACACS+]
        DUO[Cisco Duo<br/>MFA]
        AD <-->|LDAP| ISE
        ISE <-->|Auth API| DUO
    end

    subgraph Client
        UI[React UI<br/>Chat · Admin · Audit Logs]
    end

    subgraph Orchestration
        GW[Agent Gateway<br/>Auth · Rate Limit · Budget · SSE Proxy]
        CO[Coordinator Agent<br/>Claude Sonnet 4.6 · Parallel Execution · SSE]
    end

    subgraph Specialists
        NA[Network Agent<br/>Netmiko · Cisco · Palo Alto]
        RA[RAG Agent<br/>Azure AI Search]
        IA[ITSM Agent<br/>Jira]
        EA[Enrichment Agent<br/>CVE · EoX · Shodan]
    end

    subgraph Azure
        COSMOS[(Cosmos DB<br/>Conversations · Audit · Config)]
        SEARCH[(Azure AI Search<br/>Knowledge Base)]
        KV[(Key Vault<br/>Secrets)]
        FOUNDRY[Azure AI Foundry<br/>Claude Sonnet 4.6]
    end

    CF --> UI
    UI -->|SAML| ISE
    UI -->|SSE POST /chat/stream| GW
    GW --> CO
    CO --> NA & RA & IA & EA
    NA -->|TACACS+| ISE
    CO <--> COSMOS
    CO <--> FOUNDRY
    RA <--> SEARCH
    CO & GW & NA & RA & IA & EA --> KV
```

### Component Summary

| Component | Type | Purpose |
|---|---|---|
| Cloudflare | SaaS | WAF, DDoS, custom domain, origin locking |
| Active Directory | Azure VM (Windows Server 2022) | Identity store, Group Policy, DNS |
| Cisco ISE | Azure VM (Marketplace) | SAML IdP, TACACS+ device access control |
| Cisco Duo | SaaS + Auth Proxy VM | MFA — Universal Prompt, push/TOTP |
| React UI | Container (Vite) | Chat interface, admin dashboard, audit log viewer |
| Agent Gateway | Container (FastAPI) | Token validation, rate limiting, token budgets, SSE proxy |
| Coordinator Agent | Container (FastAPI + Claude Sonnet 4.6) | Multi-agent orchestration, parallel execution, SSE streaming, step-up approval gates |
| Network Agent | Container (FastAPI + Netmiko) | SSH device interrogation and config/state changes via ISE TACACS+ |
| Change Reviewer Agent | Container (FastAPI + Claude Sonnet 4.6) | AI peer review of proposed network changes — correctness, risk, alternatives |
| RAG Agent | Container (FastAPI) | Azure AI Search knowledge base queries |
| ITSM Agent | Container (FastAPI) | Jira ticket creation and querying |
| Enrichment Agent | Container (FastAPI) | CVE, EoX lifecycle, Shodan lookups |
| Cosmos DB | Azure Native | Conversations, audit logs, tenant config, change records, step-up requests/grants — partitioned by `tenant_id` |
| Azure AI Search | Azure Native | RAG knowledge base with tenant-filtered queries |
| Azure Key Vault | Azure Native | Secrets — fetched at startup via Managed Identity; write credentials fetched just-in-time post step-up approval |
| Azure Communication Services | Azure Native | Out-of-band step-up approval email notifications |
| Azure AI Foundry | Azure Native | Claude Sonnet 4.6 model hosting |

---

## Request Flow

### Authentication

```mermaid
sequenceDiagram
    actor User
    participant UI as React UI
    participant ISE as Cisco ISE
    participant AD as Active Directory
    participant Duo as Cisco Duo
    participant GW as Agent Gateway

    User->>UI: Navigate to vigil.domain.com
    UI->>ISE: Redirect (SAML SP-initiated)
    ISE->>AD: Validate username/password (LDAP)
    AD-->>ISE: User authenticated
    ISE->>Duo: MFA challenge (Universal Prompt)
    Duo-->>ISE: MFA approved
    ISE-->>UI: SAML token (contains tenant_id)
    UI->>GW: POST /chat/stream + SAML token
    GW->>GW: Validate token, extract tenant_id
```

### Multi-Agent Streaming Request

```mermaid
sequenceDiagram
    participant UI as React UI
    participant GW as Agent Gateway
    participant CO as Coordinator
    participant NA as Network Agent
    participant EA as Enrichment Agent
    participant RA as RAG Agent
    participant IA as ITSM Agent
    participant CL as Claude Sonnet 4.6

    UI->>GW: POST /chat/stream
    GW->>GW: Auth · Rate limit · Budget check
    GW->>CO: POST /chat/stream (proxy)
    CO-->>UI: SSE: session_start

    CO->>CL: Messages + tools (streaming)
    CL-->>CO: tool_use: network_agent, enrichment_agent

    CO-->>UI: SSE: agent_start (network_agent)
    CO-->>UI: SSE: agent_start (enrichment_agent)

    par Parallel execution
        CO->>NA: Query device 10.0.0.1
        NA-->>CO: Firewall config
    and
        CO->>EA: CVE/EoX lookup
        EA-->>CO: Enrichment data
    end

    CO-->>UI: SSE: agent_complete (enrichment_agent, 890ms)
    CO-->>UI: SSE: agent_complete (network_agent, 1240ms)

    CO->>CL: Tool results (streaming)
    CL-->>CO: tool_use: rag_agent, itsm_agent

    CO-->>UI: SSE: agent_start (rag_agent)
    CO-->>UI: SSE: agent_start (itsm_agent)

    par Parallel execution
        CO->>RA: Compliance lookup
        RA-->>CO: Policy references
    and
        CO->>IA: Create Jira ticket
        IA-->>CO: Ticket created
    end

    CO-->>UI: SSE: agent_complete (rag_agent, 620ms)
    CO-->>UI: SSE: agent_complete (itsm_agent, 1100ms)

    CO->>CL: All results (streaming)
    CL-->>CO: Final response tokens

    loop Token streaming
        CO-->>UI: SSE: token (content)
    end

    CO->>CO: Write Cosmos DB (conversation + audit)
    CO-->>UI: SSE: done (tokens_used, session_id)
```

---

## SSE Streaming

VIGIL uses Server-Sent Events for all chat responses. The stream flows Coordinator → Gateway (non-buffered proxy) → React UI.

### SSE Event Schema

```mermaid
stateDiagram-v2
    [*] --> session_start
    session_start --> agent_start : Claude invokes tools
    agent_start --> agent_start : parallel tools emit in succession
    agent_start --> approval_required : step-up gated tool
    approval_required --> approval_granted : approver approves
    approval_required --> approval_rejected : approver rejects
    approval_required --> approval_expired : TTL elapsed
    approval_granted --> agent_start : tool dispatches
    approval_rejected --> agent_start : coordinator continues
    approval_expired --> agent_start : coordinator continues
    agent_start --> agent_complete : agent returns
    agent_start --> agent_error : agent fails
    agent_complete --> agent_start : Claude invokes more tools
    agent_error --> agent_start : coordinator continues
    agent_complete --> token : all tools done, Claude streams
    token --> token : per token
    token --> done : Cosmos DB written
    done --> [*]
    agent_start --> error : fatal error
    error --> [*]
```

| Event | Key Fields | Description |
|---|---|---|
| `session_start` | `session_id`, `tenant_id` | Stream opened |
| `agent_start` | `agent`, `detail` | Agent about to execute (`detail` = device host or null) |
| `agent_complete` | `agent`, `duration_ms` | Agent finished — fires as each completes, not batched |
| `agent_error` | `agent`, `error` | Agent failed — coordinator continues with partial results |
| `approval_required` | `request_id`, `tool`, `context`, `approver_type`, `expires_at` | Step-up gate — loop paused awaiting human approval; keepalive heartbeats sent every 30s |
| `approval_granted` | `request_id`, `tool`, `approved_by` | Approval received, tool dispatching |
| `approval_rejected` | `request_id`, `tool`, `decided_by` | Rejected — coordinator continues with partial results |
| `approval_expired` | `request_id`, `tool` | Approval window elapsed without decision |
| `token` | `content` | One token of Claude's final response |
| `done` | `tokens_used`, `session_id` | Audit log written, stream complete |
| `error` | `code`, `message` | Fatal: `budget_exceeded`, `rate_limited`, `coordinator_unavailable` |

### Parallel Execution

When Claude returns multiple tool calls in a single response, they are independent by definition. The coordinator fans them out with `asyncio.as_completed()` — each agent completion emits its event immediately as it finishes, not after the slowest agent completes.

```
agent_start (network_agent)    ─┐ emitted in
agent_start (enrichment_agent) ─┘ immediate succession

agent_complete (enrichment_agent, 890ms)  ← finished first
agent_complete (network_agent, 1240ms)    ← finished second
```

---

## Step-Up Auth

High-risk tool calls (`apply_change`, `rollback_change`) require human approval before the coordinator dispatches them. The approval loop runs entirely inside the SSE stream — the connection stays open with keepalive heartbeats while waiting.

```mermaid
sequenceDiagram
    participant UI as React UI
    participant GW as Agent Gateway
    participant CO as Coordinator
    participant NA as Network Agent
    actor AP as Approver

    UI->>CO: POST /chat/stream ("apply the change")
    CO-->>UI: SSE: approval_required (request_id, expires_at)
    CO->>AP: Email / webhook (approve_url, reject_url)

    loop Every 30s while waiting
        CO-->>UI: SSE: ": keepalive" (comment line)
    end

    AP->>GW: POST /step-up/{id}/approve
    GW->>CO: POST /step-up/{id}/approve (X-Tenant-Id, X-User-Identity)
    CO->>CO: Validate approver, write decision, fetch write credential

    CO-->>UI: SSE: approval_granted (approved_by)
    CO-->>UI: SSE: agent_start (apply_change)
    CO->>NA: apply_change + write_credential
    NA-->>CO: Result
    CO-->>UI: SSE: agent_complete (apply_change)
```

### Approval policies (per tool, per tenant — in `tenant_config`)

| Policy field | Effect |
|---|---|
| `self_approve: false` | Requester cannot approve their own request — designated approver required |
| `self_approve: true` | Requester can approve (lower-risk tools) |
| `pending_ttl_seconds` | How long the approval window stays open (default 900s) |
| `grant_duration_seconds` | After approval, how long the grant stays active for repeat calls in the same session |

---

## Multi-Tenancy

Every layer enforces tenant isolation:

```mermaid
graph LR
    subgraph "Per-Tenant Isolation"
        A[ISE Policy Sets<br/>Separate SAML SP per tenant]
        B[Gateway<br/>tenant_id extracted from token]
        C[Cosmos DB<br/>Partitioned by tenant_id]
        D[AI Search<br/>filter: tenant_id eq x]
        E[Key Vault<br/>tenant-id-secret namespacing]
        F[Token Budgets<br/>Daily/monthly per tenant]
        G[Device Access<br/>TACACS+ policy sets per tenant]
    end
    A --> B --> C & D & E & F & G
```

Non-negotiable rules enforced in every service:
- Every Cosmos DB query uses `tenant_id` as partition key — never cross-tenant queries
- Every Azure AI Search query includes `$filter=tenant_id eq '{tenant_id}'`
- Every Key Vault secret is namespaced: `tenant-{tenant_id}-{secret-name}`
- Every audit log entry includes `tenant_id`
- Token budgets are per-tenant — checked at Gateway before Coordinator is called

---

## Security Model

```mermaid
graph TD
    L1[Cloudflare — WAF · DDoS · Origin Lock]
    L2[Cisco Duo — MFA Universal Prompt]
    L3[Active Directory — Identity Store]
    L4[Cisco ISE — SAML IdP · TACACS+ Policy]
    L5[Agent Gateway — Token Validation · Rate Limit]
    L6[Azure Managed Identity — Service-to-Service Auth]
    L1 --> L2 --> L3 --> L4 --> L5 --> L6
```

- **No stored device credentials** — network device access always flows through ISE TACACS+
- **No API keys in code or env vars** — all Azure services use Managed Identity (RBAC scoped to minimum required roles)
- **Cloudflare origin lock** — Azure Container Apps origin accepts only Cloudflare IP ranges
- **ISE logs every device command** — independent TACACS+ audit trail separate from VIGIL audit logs

---

## Technology Decisions

| Decision | Choice | Rationale |
|---|---|---|
| LLM | Claude Sonnet 4.6 via Azure AI Foundry | Azure MACC billing, Entra ID auth, best-in-class reasoning and tool use |
| Agent framework | Custom FastAPI + Claude tool use | Full control, no framework lock-in |
| Streaming | Server-Sent Events (SSE) | One-way server-to-client stream — simpler than WebSockets, sufficient for request/stream-response pattern |
| Parallelism | `asyncio.as_completed()` | Real-time per-agent events as each finishes; no external dependencies |
| UI SSE client | `fetch` + `ReadableStream` | POST support required — browser `EventSource` is GET-only |
| Database | Cosmos DB | Managed NoSQL, native Azure, serverless billing, natural tenant partitioning |
| Vector search | Azure AI Search | Managed RAG, native Foundry integration |
| Container runtime | Azure Container Apps | Managed Kubernetes abstraction, built-in ingress, revision management |
| Identity | AD + ISE + Duo | Enterprise standard stack — ISE SAML IdP + TACACS+ device access |
| Edge | Cloudflare | WAF, DDoS, custom domain, origin locking |
| IaC | Terraform | Industry standard, multi-cloud portability |
| CI/CD | GitHub Actions | Native ACR integration, path-filtered per-service deploys |

---

## Repository Structure

```
vigil-platform/
├── ARCHITECTURE.md              ← detailed architecture reference
├── CLAUDE.md                    ← Claude Code development instructions
├── services/
│   ├── gateway/                 ← FastAPI — auth, rate limiting, SSE proxy
│   ├── coordinator/             ← FastAPI + Claude Sonnet 4.6 — orchestration, streaming
│   ├── agent-network/           ← FastAPI + Netmiko — device interrogation
│   ├── agent-rag/               ← FastAPI — Azure AI Search RAG
│   ├── agent-itsm/              ← FastAPI — Jira integration
│   ├── agent-enrichment/        ← FastAPI — CVE, EoX, Shodan
│   └── ui/                      ← React + Vite — chat UI and admin
├── infrastructure/
│   └── terraform/               ← Azure IaC — modules per resource type
├── .github/
│   └── workflows/               ← GitHub Actions — path-filtered per service
├── identity/
│   ├── ise/                     ← ISE policy exports, TACACS+ command profiles
│   ├── ad/                      ← AD OU structure
│   └── duo/                     ← Duo policy documentation
└── docs/
    ├── onboarding.md
    ├── tenant-setup.md
    └── superpowers/specs/       ← design specs
```

---

## Local Development

```bash
git clone https://github.com/sjohnston1972/vigil-platform
cd vigil-platform

# Run a single service
cd services/coordinator
pip install -r requirements.txt
uvicorn main:app --reload --port 8001

# Run with Docker
docker build -t vigil-coordinator .
docker run -p 8001:8000 --env-file .env vigil-coordinator

# Terraform
cd infrastructure/terraform
terraform init
terraform plan -var-file=environments/dev.tfvars
terraform apply -var-file=environments/dev.tfvars
```

### Required Environment Variables

**Gateway**
```
COSMOS_ENDPOINT
COSMOS_DATABASE
ISE_SAML_METADATA_URL
ISE_SAML_AUDIENCE
COORDINATOR_URL
```

**Coordinator**
```
AZURE_FOUNDRY_ENDPOINT
AZURE_FOUNDRY_MODEL
COSMOS_ENDPOINT
COSMOS_DATABASE
KEY_VAULT_URL
NETWORK_AGENT_URL
RAG_AGENT_URL
ITSM_AGENT_URL
ENRICHMENT_AGENT_URL
GATEWAY_EXTERNAL_URL        # base URL for step-up approve/reject links in notifications
ACS_ENDPOINT                # Azure Communication Services endpoint (step-up emails)
ACS_SENDER_ADDRESS          # noreply address for approval emails
```

All Azure services authenticate via Managed Identity — no API keys required.
