# VIGIL Platform — Terraform Infrastructure Design

**Date:** 2026-03-17
**Scope:** Azure-native infrastructure only. VM-based identity stack (Active Directory DC, Cisco ISE, Duo Auth Proxy) is provisioned and configured manually — not covered here.
**Environment:** Production only (`prod`), UK South region.

---

## Goal

Provision all Azure resources required for the VIGIL platform before any application service is deployed. Every Container App starts with a placeholder image; GitHub Actions CI/CD replaces images on first push. After `terraform apply` completes, the platform infrastructure is ready and all services can be deployed into it without further Azure configuration.

---

## Architecture

**Approach:** Module-per-resource-type. The root module (`main.tf`) composes eight child modules, passing outputs between them as inputs. RBAC role assignments are defined at root level because they wire cross-module dependencies (e.g. Container App identity → Key Vault role). The Terraform state backend is bootstrapped manually before `terraform init` — it cannot manage itself.

**Modules:**
- `networking` — hub VNet, spoke VNet, VNet peering, subnets, NSGs, private DNS zones, private endpoints
- `container-apps` — Container Apps environment, workload profiles, all eleven Container Apps
- `cosmos-db` — Cosmos DB account, database, all six containers
- `key-vault` — Key Vault, RBAC model
- `acr` — Azure Container Registry
- `ai-search` — Azure AI Search service and index
- `ai-foundry` — Azure AI Foundry account and Claude model deployment
- `monitoring` — Log Analytics workspace, Application Insights, cost alert

**Root level:**
- `main.tf` — module composition and all RBAC role assignments
- `variables.tf` — all input variables
- `outputs.tf` — key outputs (endpoints, connection strings, URLs)
- `providers.tf` — `azurerm` and `azuread` provider versions
- `backend.tf` — Azure Storage state backend

---

## Naming Convention

Pattern: `<resource-type>-<region>-<resource-name>-<number>`

Region abbreviation: `uks` (UK South)

Storage accounts and ACR (alphanumeric only): `<type><region><name><number>` e.g. `acruksvigilprod01`, `stuksvigtfstate01`

---

## Module Designs

### networking

**Hub VNet** — `vnet-uks-hub-01`, address space `10.0.0.0/16`

| Subnet | CIDR | Purpose |
|---|---|---|
| `GatewaySubnet` | `10.0.0.0/27` | Reserved — future VPN/ExpressRoute gateway |
| `snet-uks-mgmt-01` | `10.0.1.0/24` | Future management/jump hosts |

**Spoke VNet** — `vnet-uks-vigil-01`, address space `10.1.0.0/16`

| Subnet | CIDR | Purpose |
|---|---|---|
| `snet-uks-cae-01` | `10.1.0.0/21` | Container Apps environment — `/21` minimum for Dedicated workload profile |
| `snet-uks-pe-01` | `10.1.8.0/24` | Private endpoints for all Azure PaaS services |

**VNet peering** — bidirectional between hub and spoke, `allow_forwarded_traffic = true` for future gateway transit.

**NSGs:**
- `nsg-uks-cae-01` — attached to `snet-uks-cae-01`. Default deny inbound from internet; allow inbound within subnet (inter-service traffic).
- `nsg-uks-pe-01` — attached to `snet-uks-pe-01`. Deny all inbound from internet.
- Agent-probe inbound restriction is enforced at the Container Apps ingress level (internal-only ingress) rather than NSG — Container Apps environment handles this natively.

**Private DNS zones** — all linked to the spoke VNet (`vnet-uks-vigil-01`):

| Zone | Service |
|---|---|
| `privatelink.documents.azure.com` | Cosmos DB |
| `privatelink.vaultcore.azure.net` | Key Vault |
| `privatelink.azurecr.io` | ACR |
| `privatelink.search.windows.net` | AI Search |

**Private endpoints** — one per PaaS service, all placed in `snet-uks-pe-01`, each wired to its corresponding private DNS zone. Public network access is disabled on each PaaS service after private endpoint creation.

**Module outputs:** spoke VNet ID, `snet-uks-cae-01` ID, `snet-uks-pe-01` ID, private DNS zone IDs.

---

### container-apps

**Environment** — `cae-uks-vigil-01`
- VNet-injected into `snet-uks-cae-01`
- Internal-only environment DNS suffix
- Diagnostic settings → `law-uks-vigil-01` (Log Analytics workspace)

**Workload profiles:**
- `Consumption` — default, pay-per-use, for all standard services
- `Dedicated-D4` — 4 vCPU / 16 GB RAM, required for `agent-probe` (`NET_ADMIN` + `NET_RAW` capabilities not available on Consumption)

**Container Apps:**

| App name | Workload profile | Ingress | Min/Max replicas |
|---|---|---|---|
| `ca-uks-gateway-01` | Consumption | External | 1 / 5 |
| `ca-uks-ui-01` | Consumption | External | 1 / 5 |
| `ca-uks-coordinator-01` | Consumption | Internal | 1 / 5 |
| `ca-uks-agent-network-01` | Consumption | Internal | 1 / 5 |
| `ca-uks-agent-rag-01` | Consumption | Internal | 1 / 5 |
| `ca-uks-agent-itsm-01` | Consumption | Internal | 1 / 5 |
| `ca-uks-agent-enrichment-01` | Consumption | Internal | 1 / 5 |
| `ca-uks-agent-change-reviewer-01` | Consumption | Internal | 1 / 5 |
| `ca-uks-agent-design-01` | Consumption | Internal | 1 / 5 |
| `ca-uks-agent-troubleshoot-01` | Consumption | Internal | 1 / 5 |
| `ca-uks-agent-probe-01` | Dedicated-D4 | Internal | 1 / 3 |

All Container Apps:
- **System-assigned Managed Identity** enabled
- **Placeholder image** on initial deploy: `mcr.microsoft.com/azuredocs/containerapps-helloworld` — replaced by GitHub Actions on first push
- **Environment variables** populated from Terraform outputs (Cosmos DB endpoint, Key Vault URL, internal FQDN URLs for inter-service calls, App Insights connection string, AI Foundry endpoint)

**Ingress timeout** — `ca-uks-coordinator-01` requires a 960-second HTTP ingress timeout (900s step-up pending TTL + 60s buffer). The `azurerm` Terraform provider does not expose this setting. It is set via `az containerapp ingress update` as a post-deploy step in the GitHub Actions `deploy-coordinator.yml` workflow. This is documented as a known provider gap — the Terraform module includes a comment noting it.

**Agent-probe capabilities** — `NET_ADMIN` and `NET_RAW` Linux capabilities are set on `ca-uks-agent-probe-01` via the `capabilities` block in the container spec. These are only available on Dedicated workload profiles.

**Module inputs:** CAE subnet ID, Log Analytics workspace ID, ACR login server, Key Vault URL, Cosmos DB endpoint, AI Foundry endpoint, AI Search endpoint, App Insights connection string.

**Module outputs:** FQDN of each Container App (used to populate inter-service environment variables), Managed Identity principal IDs for each app (used for RBAC assignments in root).

---

### cosmos-db

**Account** — `cosmos-uks-vigil-01`
- API: NoSQL (SQL)
- Capacity mode: Serverless
- Location: UK South
- Backup: Continuous (7-day point-in-time restore)
- Public network access: disabled (private endpoint handles access)

**Database** — `vigil`

**Containers:**

| Container | Partition key | `default_ttl` | Notes |
|---|---|---|---|
| `conversations` | `/tenant_id` | none | Session message history, keyed by `session_id` |
| `audit_logs` | `/tenant_id` | none | Every agent action; includes `tokens_used` and `budget_deducted` fields |
| `tenant_config` | `/tenant_id` | none | One document per tenant — budget limits, step-up policy, `write_enabled` flag |
| `change_records` | `/tenant_id` | none | Full lifecycle of network changes — keyed by `change_id` |
| `step_up_requests` | `/tenant_id` | none | Pending/decided approval gates — keyed by `request_id` |
| `step_up_grants` | `/tenant_id` | `-1` | Per-document TTL enabled. `default_ttl = -1` is mandatory — without it, `_ttl` fields on documents are silently ignored and grants never expire |

All containers: `indexing_mode = "consistent"`, `included_path = "/*"`.

The `step_up_requests` and `step_up_grants` containers are already defined in the existing module stub and must not be duplicated.

**Module outputs:** Cosmos DB account endpoint, account name.

---

### key-vault

**Vault** — `kv-uks-vigil-01`
- SKU: Standard
- Location: UK South
- Soft delete: enabled, 90-day retention
- Purge protection: enabled (required for prod — prevents permanent accidental deletion)
- Access model: Azure RBAC (not vault access policies)
- Public network access: disabled after private endpoint creation

**Secrets** — created manually after Terraform provisions the vault. Not managed by Terraform to avoid secrets appearing in state. The plan documents which secrets must be created and which service consumes each:

| Secret name | Consumer |
|---|---|
| `jira-api-token` | `agent-itsm` |
| `jira-base-url` | `agent-itsm` |
| `shodan-api-key` | `agent-enrichment` (optional) |
| `tenant-{id}-palo-alto-api-key` | `agent-troubleshoot` JIT fetch |
| `tenant-{id}-cisco-asa-token` | `agent-troubleshoot` JIT fetch |
| `tenant-{id}-cisco-meraki-api-key` | `agent-troubleshoot` JIT fetch |
| `tenant-{id}-fortinet-token` | `agent-troubleshoot` JIT fetch |

**Module outputs:** Key Vault URI, Key Vault ID.

---

### acr

**Registry** — `acruksvigilprod01`
- SKU: Basic
- Location: UK South
- Admin account: disabled — Container Apps pull via Managed Identity (`AcrPull`)
- Public network access: disabled (private endpoint)
- Geo-replication: none at this stage

**Module outputs:** login server FQDN, registry ID.

---

### ai-search

**Service** — `srch-uks-vigil-01`
- SKU: Basic (1 replica, 1 partition — sufficient for initial prod)
- Location: UK South
- Public network access: disabled (private endpoint)

**Index** — `vigil-knowledge`
- `tenant_id` field: filterable, not retrievable in results — used to scope RAG queries per tenant
- Remaining field schema defined during RAG knowledge base population (post-infrastructure)

**Module outputs:** search service endpoint, search service name.

---

### ai-foundry

**Account** — `aif-uks-vigil-01`
- Location: UK South
- **Note:** Verify Claude Sonnet 4.6 availability in UK South before applying. If unavailable, Sweden Central (`swedencentral`) is the recommended fallback. Model availability is not configurable — requires account in a supported region.

**Model deployment** — `claude-sonnet-4-6`
- Model: `claude-sonnet-4-6`
- TPM quota: set in `prod.tfvars` — initial value TBD based on anticipated load

**Module outputs:** AI Foundry endpoint URL.

---

### monitoring

**Log Analytics workspace** — `law-uks-vigil-01`
- Location: UK South
- Retention: 30 days (configurable via `prod.tfvars`)
- All Container Apps send logs here via the Container Apps environment diagnostic setting

**Application Insights** — `appi-uks-vigil-01`
- Linked to `law-uks-vigil-01` (workspace-based, not classic)
- Connection string passed to all Container Apps as `APPLICATIONINSIGHTS_CONNECTION_STRING` environment variable

**Cost Management budget alert** — scoped to `rg-uks-vigil-01`
- Alert at 80% and 100% of monthly threshold
- Threshold value set in `prod.tfvars`
- Notification email set in `prod.tfvars`

**Module outputs:** Log Analytics workspace ID, App Insights connection string.

---

## RBAC Role Assignments

Defined in root `main.tf`. All assignments use Managed Identity principal IDs output from the `container-apps` module.

**Container App → Azure service:**

| Role | Scope | Assigned to |
|---|---|---|
| `Cosmos DB Built-in Data Contributor` | Cosmos DB account | gateway, coordinator, agent-network, agent-itsm, agent-enrichment, agent-change-reviewer, agent-design, agent-troubleshoot |
| `Key Vault Secrets User` | Key Vault | agent-itsm, agent-enrichment, agent-troubleshoot, coordinator |
| `Search Index Data Contributor` | AI Search | agent-rag, agent-design |
| `Cognitive Services OpenAI User` | AI Foundry | coordinator, agent-change-reviewer, agent-design, agent-troubleshoot |
| `AcrPull` | ACR | all eleven Container Apps |

**GitHub Actions service principal** — `sp-uks-github-vigil-01` (created via `azuread` provider):

| Role | Scope |
|---|---|
| `AcrPush` | ACR |
| `Azure Container Apps Contributor` | `rg-uks-vigil-01` |

The service principal client ID and secret are stored as GitHub Actions repository secrets (`AZURE_CREDENTIALS`, `REGISTRY_LOGIN_SERVER`, `REGISTRY_USERNAME`, `REGISTRY_PASSWORD`) — created manually after Terraform runs, not stored in Terraform state.

---

## Resource Group

`rg-uks-vigil-01` — contains all VIGIL resources. All resources tagged:

```hcl
tags = {
  environment = "prod"
  platform    = "vigil"
  managed_by  = "terraform"
}
```

---

## State Backend Bootstrap

The Terraform state backend storage account must exist before `terraform init` can run. It is provisioned manually (Azure CLI) as a one-time prerequisite:

```bash
az group create --name rg-uks-tfstate-01 --location uksouth
az storage account create --name stuksvigtfstate01 --resource-group rg-uks-tfstate-01 \
  --location uksouth --sku Standard_LRS
az storage container create --name tfstate --account-name stuksvigtfstate01
```

`backend.tf`:
```hcl
terraform {
  backend "azurerm" {
    resource_group_name  = "rg-uks-tfstate-01"
    storage_account_name = "stuksvigtfstate01"
    container_name       = "tfstate"
    key                  = "vigil.prod.terraform.tfstate"
  }
}
```

The state storage account lives in its own resource group (`rg-uks-tfstate-01`) separate from the platform resources — so `terraform destroy` on the platform cannot accidentally delete the state.

---

## Environment Configuration

`environments/prod.tfvars`:
```hcl
location                  = "uksouth"
resource_group_name       = "rg-uks-vigil-01"
cosmos_database_name      = "vigil"
acr_name                  = "acruksvigilprod01"
ai_foundry_model_tpm      = 100000          # tokens per minute quota
log_retention_days        = 30
monthly_budget_gbp        = 500             # cost alert threshold
budget_alert_email        = ""              # set before apply
environment               = "prod"
```

---

## Apply Order

Terraform resolves dependencies automatically via the resource graph. The effective apply order is:

1. Resource group
2. State backend (manual prerequisite — already done)
3. Networking (VNets, subnets, NSGs, DNS zones)
4. ACR, Key Vault, Cosmos DB, AI Search, AI Foundry, Monitoring (parallel — no interdependencies)
5. Private endpoints (depend on PaaS resources + networking)
6. Container Apps environment (depends on networking)
7. Container Apps (depend on environment + all PaaS endpoints)
8. RBAC role assignments (depend on Container App identities + PaaS resource IDs)
9. GitHub Actions service principal (depends on ACR + resource group)

A single `terraform apply` handles all of this. No manual ordering required.

---

## Known Terraform Provider Gaps

| Gap | Workaround |
|---|---|
| `azurerm` does not expose Container Apps ingress timeout | Set via `az containerapp ingress update` in `deploy-coordinator.yml` GitHub Actions workflow |
| `azurerm` does not manage Key Vault secrets (intentional) | Secrets created manually post-apply; documented in plan |
| GitHub Actions secrets cannot be set via Terraform | Set manually after service principal creation |
