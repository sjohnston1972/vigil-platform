# CLAUDE.md — VIGIL Platform

This file instructs Claude Code how to work within the VIGIL codebase. Claude Code reads this file automatically when opening this repo. Read it fully before making any changes.

Per-service `CLAUDE.md` files exist in `services/gateway/` and `services/coordinator/` — Claude Code merges all of them into context automatically.

---

## Project Overview

VIGIL (Visibility and Intelligence for Governed Infrastructure and Lifecycle) is a multi-tenant, AI-powered managed services platform built on Azure. It uses a coordinator agent pattern — a central coordinator (Claude Sonnet 4.6) orchestrates specialist agents across network, RAG, ITSM, and enrichment domains. All services are containerised and deployed to Azure Container Apps via GitHub Actions CI/CD.

**Full architecture:** See `ARCHITECTURE.md` — read it before building anything significant.

---

## Repository Structure

```
vigil-platform/
├── ARCHITECTURE.md          ← architecture reference
├── CLAUDE.md                ← this file
├── services/
│   ├── gateway/             ← FastAPI — auth, rate limiting, routing
│   ├── coordinator/         ← FastAPI + Claude Sonnet 4.6 — orchestration
│   ├── agent-network/       ← FastAPI + Netmiko — device interrogation
│   ├── agent-rag/           ← FastAPI — Azure AI Search RAG
│   ├── agent-itsm/          ← FastAPI — Jira integration
│   ├── agent-enrichment/    ← FastAPI — CVE, EoX, Shodan
│   └── ui/                  ← React + Vite — chat UI and admin
├── infrastructure/
│   └── terraform/           ← Terraform IaC for all Azure resources
├── .github/
│   └── workflows/           ← GitHub Actions CI/CD
└── docs/                    ← operational documentation
```

---

## Coding Standards

### Python services

- Python 3.11
- FastAPI for all HTTP services
- Pydantic models for all request and response schemas — no raw dicts in API contracts
- Azure Managed Identity for all Azure service auth — never hardcode credentials
- Fetch secrets from Azure Key Vault at startup, not at request time
- Environment variables via `python-dotenv` locally, Container Apps env vars when deployed
- Structured logging with `tenant_id` on every log entry where available
- Type hints on all function signatures
- Docstrings on all public functions

### File naming
- Python: `snake_case.py`
- React components: `PascalCase.tsx`
- GitHub Actions: `deploy-{service}.yml`
- Terraform: standard `main.tf`, `variables.tf`, `outputs.tf`

### Error handling
- All endpoints return structured Pydantic error responses — never raw exceptions
- Specialist agents handle connection failures and return structured errors
- Coordinator handles specialist failures gracefully — partial results over full failures

---

## Service Patterns

### Gateway auth middleware — `services/gateway/middleware/auth.py`

The Gateway validates ISE-issued SAML tokens on every request. This is the security boundary of the entire platform. Rules:

- Validate token signature against ISE SAML metadata
- Extract `tenant_id` from token claims — this is the source of truth for tenant identity
- Reject any request with an invalid, expired, or missing token with HTTP 401
- Never modify this middleware without explicit instruction — it is the security boundary

```python
import os
import logging
from fastapi import FastAPI
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

@app.get("/health")
def health():
    return {"status": "healthy", "service": "service-name"}
```

### Health endpoint is mandatory on every service
Azure Container Apps uses `/health` for health checks. Missing it causes deployment failures.

### Pydantic request/response pattern

```python
class AuditRequest(BaseModel):
    tenant_id: str
    session_id: str
    device_host: str
    query: str

class AuditResponse(BaseModel):
    tenant_id: str
    findings: list[dict]
    error: str | None = None
```

### Logging — always include tenant_id

```python
logger.info("Audit request received", extra={
    "tenant_id": request.tenant_id,
    "device_host": request.device_host
})
```

---

## Multi-Tenancy Rules — Non-Negotiable

Every piece of code must respect these rules. There are no exceptions.

1. **Every request carries `tenant_id`** — extracted from Cloudflare JWT by the Gateway, passed on every downstream call
2. **Every Cosmos DB query filters by `tenant_id`** — never query across tenants
3. **Every Key Vault secret is namespaced** — format: `tenant-{tenant_id}-{secret-name}`
4. **Every Azure AI Search query includes tenant filter** — `$filter=tenant_id eq '{tenant_id}'`
5. **Every audit log entry includes `tenant_id`** — no exceptions
6. **Token budgets are per-tenant** — check tenant budget, never a global budget

If you are writing code that touches data and there is no `tenant_id` filter — stop. That is a multi-tenancy violation.

---

## Cosmos DB Patterns

### Containers
- `conversations` — partitioned by `tenant_id`, keyed by `session_id`
- `audit_logs` — partitioned by `tenant_id`, keyed by timestamp
- `tenant_config` — partitioned by `tenant_id`, one document per tenant
- `change_records` — partitioned by `tenant_id`, keyed by `change_id` — full lifecycle of every proposed network change
- `step_up_requests` — partitioned by `tenant_id`, keyed by `request_id` — pending/decided approval gates
- `step_up_grants` — partitioned by `tenant_id`, `default_ttl = -1` required — active time-window grants (TTL = `grant_duration_seconds`)

### Always use partition key

```python
# CORRECT
container.read_item(item=session_id, partition_key=tenant_id)

# WRONG — cross-partition, expensive, returns all tenants' data
container.query_items(query="SELECT * FROM c WHERE c.session_id = @id")
```

### Conversation document structure

```python
{
    "id": session_id,
    "tenant_id": tenant_id,
    "created_at": "2026-01-01T00:00:00Z",
    "updated_at": "2026-01-01T00:00:00Z",
    "messages": [
        {"role": "system", "content": "..."},
        {"role": "user", "content": "..."},
        {"role": "assistant", "content": "..."}
    ],
    "token_count": 1240
}
```

---

## Azure Key Vault Pattern

Fetch at startup using Managed Identity. Never at request time.

```python
from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient

credential = DefaultAzureCredential()
kv_client = SecretClient(
    vault_url=os.getenv("KEY_VAULT_URL"),
    credential=credential
)

# Fetch once at startup, cache in module scope
JIRA_API_TOKEN = kv_client.get_secret("jira-api-token").value
```

**Exception — step-up write credentials:** Write credentials for step-up gated tools
(`apply_change`, `rollback_change`, etc.) are fetched just-in-time after approval, not at
startup, and are not cached in module scope. This is the only approved exception to the
startup-fetch rule. All other secrets follow the startup-fetch pattern above.

---

## SSE Streaming Patterns

### Coordinator — streaming endpoint

Every chat request uses the streaming endpoint. The non-streaming `POST /chat` exists as a fallback only.

```python
from fastapi.responses import StreamingResponse
import json

@app.post("/chat/stream")
async def chat_stream(request: ChatRequest) -> StreamingResponse:
    return StreamingResponse(
        _stream_generator(request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        }
    )

def _sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"
```

### Coordinator — parallel agent execution with real-time SSE

Use `asyncio.as_completed()` — not `asyncio.gather()`. `gather()` batches completions; `as_completed()` emits each event immediately as each agent finishes.

```python
import asyncio

async def _stream_generator(request: ChatRequest):
    yield _sse({"type": "session_start", "session_id": request.session_id, "tenant_id": request.tenant_id})

    # When Claude returns multiple tool_use blocks, fan out with as_completed()
    tool_calls = [network_agent_call(...), enrichment_agent_call(...)]

    # Emit all agent_start events before any execution begins
    for call in tool_calls:
        yield _sse({"type": "agent_start", "agent": call.agent_name, "detail": call.detail})

    # Execute concurrently, emit completion events as each finishes
    futures = {asyncio.ensure_future(call.run()): call for call in tool_calls}
    for future in asyncio.as_completed(futures):
        call = futures[future]
        try:
            result = await future
            yield _sse({"type": "agent_complete", "agent": call.agent_name, "duration_ms": result.duration_ms})
        except Exception as e:
            yield _sse({"type": "agent_error", "agent": call.agent_name, "error": str(e)})

    # Stream Claude's final response token by token
    async for token in claude_stream_response(...):
        yield _sse({"type": "token", "content": token})

    # Write to Cosmos DB BEFORE emitting done — audit integrity guarantee
    await write_conversation_and_audit(request, total_tokens)

    yield _sse({"type": "done", "tokens_used": total_tokens, "session_id": request.session_id})
```

### Gateway — non-buffered SSE proxy

The Gateway proxies the Coordinator's stream without buffering. It scans for the `done` event to capture `tokens_used` for budget accounting.

```python
import httpx

async def _proxy_stream(request: ChatRequest, tenant_id: str):
    async with httpx.AsyncClient() as client:
        async with client.stream("POST", COORDINATOR_URL + "/chat/stream", json=request.dict()) as response:
            tokens_used = None
            async for chunk in response.aiter_bytes():
                if b'"type": "done"' in chunk:
                    try:
                        data = json.loads(chunk.split(b"data: ", 1)[1])
                        tokens_used = data.get("tokens_used")
                    except Exception:
                        pass
                yield chunk
            if tokens_used is not None:
                await update_tenant_budget(tenant_id, tokens_used)
            else:
                await log_incomplete_session(tenant_id, request.session_id)

@app.post("/chat/stream")
async def chat_stream_proxy(request: ChatRequest, tenant_id: str = Depends(extract_tenant)):
    # auth, rate limit, budget checks happen before this point
    return StreamingResponse(
        _proxy_stream(request, tenant_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )
```

### UI — SSE client (fetch, not EventSource)

**Never use `EventSource`** — it only supports GET requests. Use `fetch` with `ReadableStream`.

```typescript
// services/ui/src/hooks/useStream.ts
async function* streamChat(request: ChatRequest): AsyncGenerator<SSEEvent> {
    const response = await fetch('/chat/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(request),
    });
    const reader = response.body!.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n\n');
        buffer = lines.pop()!;
        for (const line of lines) {
            if (line.startsWith('data: ')) {
                yield JSON.parse(line.slice(6)) as SSEEvent;
            }
        }
    }
}
```

### SSE event types

| Event | Fields | UI action |
|---|---|---|
| `session_start` | `session_id`, `tenant_id` | Initialise sidebar entry for this message |
| `agent_start` | `agent`, `detail` | Add agent row to sidebar (amber dot) |
| `agent_complete` | `agent`, `duration_ms` | Update row (green dot, timing) |
| `agent_error` | `agent`, `error` | Update row (red dot, error) |
| `token` | `content` | Append token to message bubble |
| `done` | `tokens_used`, `session_id` | Finalise message, update token counter |
| `error` | `code`, `message` | Show error in message bubble, close stream |

---

## Coordinator Tool Registration Pattern

Every specialist agent is a Claude tool in the coordinator. Follow this exactly:

```python
tools = [
    {
        "name": "network_agent",
        "description": "Connects to network devices and retrieves configuration, interface status, routing tables, and ACLs. Use when the user asks about device configuration, connectivity, or network state.",
        "input_schema": {
            "type": "object",
            "properties": {
                "device_host": {
                    "type": "string",
                    "description": "IP address or hostname of the target device"
                },
                "query_type": {
                    "type": "string",
                    "enum": ["running_config", "interfaces", "routing_table", "acl"],
                    "description": "Type of information to retrieve"
                }
            },
            "required": ["device_host", "query_type"]
        }
    }
]
```

Tool descriptions must be precise — Claude uses them to decide which agent to invoke. Vague descriptions cause wrong agent selection.

---

## Terraform Patterns

### State backend — always configure this first

```hcl
terraform {
  backend "azurerm" {
    resource_group_name  = "rg-vigil-tfstate"
    storage_account_name = "vigiltfstate"
    container_name       = "tfstate"
    key                  = "vigil.terraform.tfstate"
  }
}
```

### Module structure — one module per Azure resource type

```
infrastructure/terraform/modules/
├── container-apps/
│   ├── main.tf
│   ├── variables.tf
│   └── outputs.tf
├── cosmos-db/
├── ai-search/
└── key-vault/
```

### Environment separation

```bash
# Deploy to dev
terraform apply -var-file=environments/dev.tfvars

# Deploy to prod
terraform apply -var-file=environments/prod.tfvars
```

### Never hardcode values in Terraform
All resource names, locations, SKUs, and secrets must be variables. No hardcoded values in `main.tf`.

### Tag every resource

```hcl
tags = {
  environment = var.environment
  platform    = "vigil"
  managed_by  = "terraform"
}
```

---

## Docker Patterns

### Standard Dockerfile — copy for every Python service

```dockerfile
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

Do not change base image, port, or CMD without updating all services.

---

## GitHub Actions Patterns

### Path-filtered deploy — copy for every service

```yaml
name: Deploy {Service Name}

on:
  push:
    branches: [main]
    paths:
      - 'services/{service-folder}/**'

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3

      - name: Log in to Azure
        uses: azure/login@v1
        with:
          creds: ${{ secrets.AZURE_CREDENTIALS }}

      - name: Log in to ACR
        uses: azure/docker-login@v1
        with:
          login-server: ${{ secrets.REGISTRY_LOGIN_SERVER }}
          username: ${{ secrets.REGISTRY_USERNAME }}
          password: ${{ secrets.REGISTRY_PASSWORD }}

      - name: Build and push
        run: |
          docker build -t ${{ secrets.REGISTRY_LOGIN_SERVER }}/vigil-{service}:${{ github.sha }} services/{service-folder}/
          docker push ${{ secrets.REGISTRY_LOGIN_SERVER }}/vigil-{service}:${{ github.sha }}

      - name: Deploy to Container Apps
        run: |
          az containerapp update \
            --name vigil-{service} \
            --resource-group rg-vigil-prod \
            --image ${{ secrets.REGISTRY_LOGIN_SERVER }}/vigil-{service}:${{ github.sha }}
```

The `paths:` filter is important — each workflow only triggers when its own service changes.

---

## Adding a New Specialist Agent — Checklist

Follow these steps in order. Do not skip any.

- [ ] Create `services/agent-{name}/` folder
- [ ] Create `main.py` with FastAPI, `/health` endpoint, primary endpoint
- [ ] Create `requirements.txt`
- [ ] Create `Dockerfile` — copy from existing agent, change service name only
- [ ] Register as a tool in `services/coordinator/tools/`
- [ ] Add agent internal URL as env var in coordinator
- [ ] Create `deploy-agent-{name}.yml` in `.github/workflows/`
- [ ] Update `ARCHITECTURE.md` Component Reference
- [ ] Update this file if new patterns are introduced

---

## Testing Requirements

Every service needs tests before merging to `main`.

Minimum per service:
- Health endpoint returns 200
- Primary endpoint returns correct response structure
- Auth validation rejects invalid tokens
- Tenant isolation — tenant A cannot access tenant B's data

```bash
cd services/{service-name}
pip install -r requirements.txt
pytest tests/
```

---

## Network Agent Write Operations Pattern

### Two-phase change flow

Phase 1 (propose) and Phase 2 (apply) are separate requests. Nothing touches the device in Phase 1.

```python
# Phase 1: propose_change — generates diff, writes change record, no device changes
# Phase 2: apply_change — validates approved status, applies, verifies

# ALWAYS fetch change_commands from the stored change record at apply time
# NEVER execute commands from the tool call input at apply time
record = container.read_item(item=change_id, partition_key=tenant_id)
if record["status"] != "approved":
    raise ValueError("invalid_change_status")
change_commands = record["change_commands"]  # from DB, not from tool input
```

### change_records Cosmos DB lookups — always use partition key

```python
# CORRECT — tenant-scoped lookup
record = container.read_item(item=change_id, partition_key=tenant_id)
if record["tenant_id"] != tenant_id:
    raise ValueError("unauthorized")

# WRONG — cross-partition query violates multi-tenancy
container.query_items(query="SELECT * FROM c WHERE c.id = @id")
```

### Change record state machine

```
pending → reviewed → approved → applying → applied
                              ↘ rejected  ↓          ↘ failed → rolled_back
                                          ↓
                                    drift_pending → approved → applying → ...
                                          ↘ failed (user cancelled)
```

- `apply_change` only proceeds if `status == "approved"` — `drift_pending` is rejected
- `rollback_change` only proceeds if `status == "applied"` or `"failed"`
- Pre-change config is written to the record **before** any commands are sent to the device

### Change Reviewer Agent — data handling

```python
# current_config and proposed_change are NEVER logged or stored
# Used only for the Claude review call, then discarded
logger.info("Review requested", extra={
    "tenant_id": request.tenant_id,
    "change_id": request.change_id,
    "device_type": request.device_type,
    # DO NOT log current_config or proposed_change
})
```

### Stuck `applying` recovery

The Network Agent runs a background task that checks for change records stuck in `applying` status using the `applying_started_at` Cosmos DB timestamp (not in-memory state — must survive container restarts):

```python
# On startup and periodically
stuck = container.query_items(
    query="SELECT * FROM c WHERE c.status = 'applying' AND c.applying_started_at < @cutoff",
    parameters=[{"name": "@cutoff", "value": cutoff_iso}],
    partition_key=tenant_id  # per-tenant scan
)
for record in stuck:
    record["status"] = "failed"
    record["failure_reason"] = "apply_timeout"
    container.replace_item(item=record["id"], body=record, partition_key=tenant_id)
```

---

## What Claude Code Must Never Do

- Hardcode credentials, API keys, or connection strings anywhere
- Query Cosmos DB without a `tenant_id` filter
- Bypass the Gateway — all requests flow through ISE token validation and rate limiting
- Modify Gateway auth middleware without explicit instruction — this is the security boundary
- Store device credentials anywhere — device auth always flows through ISE TACACS+
- Push directly to `main` — always use a feature branch and PR
- Change Dockerfile base image without updating all services
- Skip the `/health` endpoint on any new service
- Leave `ARCHITECTURE.md` or `CLAUDE.md` out of date after changing the system
- Hardcode values in Terraform — use variables
- Use `EventSource` for SSE — it is GET-only and cannot send a request body; always use `fetch` + `ReadableStream`
- Execute `change_commands` from the tool call input at apply time — always fetch from the stored `change_records` document
- Query `change_records` without `tenant_id` as partition key — always use `(change_id, tenant_id)`
- Log or store `current_config` or `proposed_change` in the Change Reviewer Agent — these are used only during the Claude review call
- Proceed with `apply_change` if `status != "approved"` — `drift_pending`, `pending`, and all other statuses must be rejected
- Emit `done` before writing the Cosmos DB audit/conversation record — write ordering guarantees audit integrity
- Use `asyncio.gather()` for parallel agent calls — use `asyncio.as_completed()` so `agent_complete` events emit in real time as each agent finishes, not batched after the slowest
- Emit `done` before writing to Cosmos DB — the write must complete first to guarantee audit integrity
- Buffer the SSE stream in the Gateway — use `httpx.AsyncClient` with `aiter_bytes()` and `StreamingResponse`

---

## Local Development

```bash
git clone https://github.com/sjohnston1972/vigil-platform
cd vigil-platform

# Run a single service
cd services/gateway
pip install -r requirements.txt
uvicorn main:app --reload --port 8000

# Run with Docker
docker build -t vigil-gateway .
docker run -p 8000:8000 --env-file .env vigil-gateway

# Terraform
cd infrastructure/terraform
terraform init
terraform plan -var-file=environments/dev.tfvars
terraform apply -var-file=environments/dev.tfvars
```

---

## Prompting Claude Code Effectively

Always provide:
1. Which service you are working in
2. What the feature should do
3. Which other services or Azure resources it interacts with
4. How `tenant_id` should be handled

Example:
> "In services/agent-enrichment, add endpoint `/lookup/eox` that accepts a Cisco product ID and tenant_id, queries the Cisco EoX API, and returns structured lifecycle data. API key is in Key Vault as `cisco-support-api-key`."

VIGIL has strong patterns — follow them, don't invent new ones.
