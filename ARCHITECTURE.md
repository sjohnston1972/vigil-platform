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
  - Enforce per-tenant token budgets (daily/monthly)
  - Route validated requests to the Coordinator Agent
  - Write audit log entries to Cosmos DB
  - Return structured errors for rate limit, auth, and budget violations
- **Does not:** Execute agent logic, call LLMs, or access network devices

### Coordinator Agent
- **Type:** Containerised (FastAPI + Claude Sonnet 4.6)
- **Service:** `services/coordinator`
- **Responsibilities:**
  - Receive validated requests from the Gateway
  - Load conversation history from Cosmos DB using session ID
  - Determine which specialist agents to invoke and in what order
  - Execute multi-step agent workflows via tool calls
  - Handle specialist agent failures gracefully
  - Assemble consolidated responses
  - Write updated conversation history to Cosmos DB
- **Model:** Claude Sonnet 4.6 via Azure AI Foundry
- **Tool definitions:** One tool registered per specialist agent

### Network Agent
- **Type:** Containerised (FastAPI + Netmiko)
- **Service:** `services/agent-network`
- **Responsibilities:**
  - Authenticate to network devices via ISE TACACS+ — no direct credential storage
  - Connect to network devices via SSH using Netmiko
  - Pull running configurations, interface status, routing tables, ACLs
  - Parse and structure device output for downstream agents
  - ISE enforces which devices the agent can access and which commands it can run
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

### Standard conversational request

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
Coordinator Agent (loads conversation history from Cosmos DB)
        ↓
[Specialist agents as needed]
        ↓
Azure AI Foundry (Claude Sonnet 4.6)
        ↓
Coordinator assembles response → Cosmos DB (history + audit log)
        ↓
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

### Multi-agent audit workflow

```
User: "Audit the perimeter firewall and raise a ticket for critical findings"

Coordinator:
  Step 1 → Network Agent → pulls firewall config via SSH
  Step 2 → Enrichment Agent (with findings) → CVE lookup, EoX check
  Step 3 → RAG Agent (with critical findings) → compliance doc lookup
  Step 4 → ITSM Agent (critical findings only) → raises Jira ticket
  Assembles → consolidated report with ticket reference
```

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
| Token budgets | Per-tenant daily/monthly limits enforced at Gateway |
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
```

**Coordinator**
```
AZURE_FOUNDRY_ENDPOINT  # Azure AI Foundry endpoint
AZURE_FOUNDRY_MODEL     # Model deployment name (claude-sonnet-4-6)
NETWORK_AGENT_URL       # Internal URL of Network Agent
RAG_AGENT_URL           # Internal URL of RAG Agent
ITSM_AGENT_URL          # Internal URL of ITSM Agent
ENRICHMENT_AGENT_URL    # Internal URL of Enrichment Agent
COSMOS_ENDPOINT         # Azure Cosmos DB endpoint
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
