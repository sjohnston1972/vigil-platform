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
