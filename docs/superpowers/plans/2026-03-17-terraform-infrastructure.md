# Terraform Infrastructure Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Provision all Azure resources required for the VIGIL platform so that Container Apps exist with placeholder images and CI/CD can deploy services on first push.

**Architecture:** Module-per-resource-type (eight modules). Root `main.tf` composes them, passing outputs as inputs. RBAC role assignments live at root level because they wire cross-module dependencies. Private endpoints live in each PaaS module (takes PE subnet ID + DNS zone ID from networking). Networking module outputs PE subnet ID and DNS zone IDs consumed by PaaS modules.

**Tech Stack:** Terraform >= 1.7, azurerm ~> 3.110, azuread ~> 2.50, Azure UK South.

---

## File Map

| Path | Action | Responsibility |
|---|---|---|
| `infrastructure/terraform/backend.tf` | Create | State backend configuration |
| `infrastructure/terraform/providers.tf` | Create | Provider versions and configuration |
| `infrastructure/terraform/variables.tf` | Create | All root input variables |
| `infrastructure/terraform/outputs.tf` | Create | Key platform outputs |
| `infrastructure/terraform/main.tf` | Create | Module composition + RBAC assignments |
| `infrastructure/terraform/environments/prod.tfvars` | Create | Production values |
| `infrastructure/terraform/modules/monitoring/main.tf` | Create | Log Analytics, App Insights, cost alert |
| `infrastructure/terraform/modules/monitoring/variables.tf` | Create | Monitoring module inputs |
| `infrastructure/terraform/modules/monitoring/outputs.tf` | Create | workspace ID, App Insights connection string |
| `infrastructure/terraform/modules/networking/main.tf` | Create | VNets, subnets, NSGs, peering, DNS zones |
| `infrastructure/terraform/modules/networking/variables.tf` | Create | Networking module inputs |
| `infrastructure/terraform/modules/networking/outputs.tf` | Create | Subnet IDs, DNS zone ID map |
| `infrastructure/terraform/modules/cosmos-db/main.tf` | **Modify** | Add account, database, 4 containers, PE — DO NOT duplicate existing step_up_* containers |
| `infrastructure/terraform/modules/cosmos-db/variables.tf` | **Modify** | Add pe_subnet_id, private_dns_zone_id |
| `infrastructure/terraform/modules/cosmos-db/outputs.tf` | **Modify** | Add cosmos_endpoint, cosmos_account_name |
| `infrastructure/terraform/modules/key-vault/main.tf` | Create | Key Vault + private endpoint |
| `infrastructure/terraform/modules/key-vault/variables.tf` | Create | Key Vault module inputs |
| `infrastructure/terraform/modules/key-vault/outputs.tf` | Create | key_vault_uri, key_vault_id |
| `infrastructure/terraform/modules/acr/main.tf` | Create | ACR Premium + private endpoint |
| `infrastructure/terraform/modules/acr/variables.tf` | Create | ACR module inputs |
| `infrastructure/terraform/modules/acr/outputs.tf` | Create | login_server, registry_id |
| `infrastructure/terraform/modules/ai-search/main.tf` | Create | AI Search service, index, private endpoint |
| `infrastructure/terraform/modules/ai-search/variables.tf` | Create | AI Search module inputs |
| `infrastructure/terraform/modules/ai-search/outputs.tf` | Create | search_endpoint, search_service_name |
| `infrastructure/terraform/modules/ai-foundry/main.tf` | Create | AI Foundry account, model deployment, PE |
| `infrastructure/terraform/modules/ai-foundry/variables.tf` | Create | AI Foundry module inputs |
| `infrastructure/terraform/modules/ai-foundry/outputs.tf` | Create | ai_foundry_endpoint |
| `infrastructure/terraform/modules/container-apps/main.tf` | **Modify** | Add CAE, workload profiles, all 11 apps |
| `infrastructure/terraform/modules/container-apps/variables.tf` | **Modify** | Add all module inputs |
| `infrastructure/terraform/modules/container-apps/outputs.tf` | Create | FQDNs and Managed Identity principal IDs |

---

### Task 1: Bootstrap — State Backend and Providers

**Files:**
- Create: `infrastructure/terraform/backend.tf`
- Create: `infrastructure/terraform/providers.tf`

- [ ] **Step 1: Create the state backend storage (one-time, manual — Azure CLI)**

Run this from your terminal with an active Azure login. Skip if already done.

```bash
az group create --name rg-uks-tfstate-01 --location uksouth
az storage account create --name stuksvigtfstate01 --resource-group rg-uks-tfstate-01 \
  --location uksouth --sku Standard_LRS
az storage container create --name tfstate --account-name stuksvigtfstate01
```

- [ ] **Step 2: Write `infrastructure/terraform/backend.tf`**

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

- [ ] **Step 3: Write `infrastructure/terraform/providers.tf`**

```hcl
terraform {
  required_version = ">= 1.7"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 3.110"
    }
    azuread = {
      source  = "hashicorp/azuread"
      version = "~> 2.50"
    }
  }
}

provider "azurerm" {
  features {}
}

provider "azuread" {}
```

- [ ] **Step 4: Run `terraform init` to connect to the state backend and download providers**

```bash
cd infrastructure/terraform
terraform init
```

Expected: `Terraform has been successfully initialized!`

- [ ] **Step 5: Commit**

```bash
git add infrastructure/terraform/backend.tf infrastructure/terraform/providers.tf
git commit -m "feat(terraform): add state backend and provider config"
```

---

### Task 2: Root Scaffold — Resource Group, Variables, Outputs, Skeleton

**Files:**
- Create: `infrastructure/terraform/variables.tf`
- Create: `infrastructure/terraform/outputs.tf`
- Create: `infrastructure/terraform/main.tf`
- Create: `infrastructure/terraform/environments/prod.tfvars`

- [ ] **Step 1: Write `infrastructure/terraform/variables.tf`**

```hcl
variable "location" {
  type        = string
  description = "Azure region for all resources"
}

variable "resource_group_name" {
  type        = string
  description = "Name of the main VIGIL resource group"
}

variable "cosmos_database_name" {
  type        = string
  description = "Name of the Cosmos DB database"
}

variable "acr_name" {
  type        = string
  description = "Name of the Azure Container Registry (alphanumeric only)"
}

variable "ai_foundry_model_tpm" {
  type        = number
  description = "Tokens-per-minute quota for the Claude Sonnet 4.6 model deployment"
}

variable "log_retention_days" {
  type        = number
  description = "Log Analytics workspace retention in days"
  default     = 30
}

variable "monthly_budget_gbp" {
  type        = number
  description = "Monthly cost alert threshold in GBP"
}

variable "budget_alert_email" {
  type        = string
  description = "Email address for cost alert notifications — required before apply"
  validation {
    condition     = length(var.budget_alert_email) > 0
    error_message = "budget_alert_email must not be empty. Set it in environments/prod.tfvars before running terraform apply."
  }
}

variable "environment" {
  type        = string
  description = "Environment name (e.g. prod)"
  default     = "prod"
}
```

- [ ] **Step 2: Write `infrastructure/terraform/environments/prod.tfvars`**

```hcl
location             = "uksouth"
resource_group_name  = "rg-uks-vigil-01"
cosmos_database_name = "vigil"
acr_name             = "acruksvigilprod01"
ai_foundry_model_tpm = 100000
log_retention_days   = 30
monthly_budget_gbp   = 500
budget_alert_email   = "alerts@example.com"   # REQUIRED: replace before terraform apply
environment          = "prod"
```

- [ ] **Step 3: Write `infrastructure/terraform/main.tf` (resource group + locals only; module calls added in later tasks)**

```hcl
locals {
  tags = {
    environment = var.environment
    platform    = "vigil"
    managed_by  = "terraform"
  }
}

resource "azurerm_resource_group" "vigil" {
  name     = var.resource_group_name
  location = var.location
  tags     = local.tags
}
```

- [ ] **Step 4: Write `infrastructure/terraform/outputs.tf` (placeholder — extended in later tasks)**

```hcl
# Outputs are added as modules are wired into main.tf
```

- [ ] **Step 5: Validate**

```bash
cd infrastructure/terraform
terraform validate
```

Expected: `Success! The configuration is valid.`

- [ ] **Step 6: Commit**

```bash
git add infrastructure/terraform/variables.tf infrastructure/terraform/outputs.tf \
        infrastructure/terraform/main.tf infrastructure/terraform/environments/prod.tfvars
git commit -m "feat(terraform): add root scaffold, variables, and resource group"
```

---

### Task 3: Monitoring Module

**Files:**
- Create: `infrastructure/terraform/modules/monitoring/variables.tf`
- Create: `infrastructure/terraform/modules/monitoring/main.tf`
- Create: `infrastructure/terraform/modules/monitoring/outputs.tf`
- Modify: `infrastructure/terraform/main.tf`

- [ ] **Step 1: Write `modules/monitoring/variables.tf`**

```hcl
variable "resource_group_name" {
  type        = string
  description = "Name of the Azure resource group"
}

variable "location" {
  type        = string
  description = "Azure region"
}

variable "log_retention_days" {
  type        = number
  description = "Log Analytics workspace retention in days"
}

variable "monthly_budget_gbp" {
  type        = number
  description = "Monthly budget alert threshold in GBP"
}

variable "budget_alert_email" {
  type        = string
  description = "Email address for cost alert notifications"
}

variable "tags" {
  type        = map(string)
  description = "Tags to apply to all resources"
  default     = {}
}
```

- [ ] **Step 2: Write `modules/monitoring/main.tf`**

```hcl
resource "azurerm_log_analytics_workspace" "vigil" {
  name                = "law-uks-vigil-01"
  location            = var.location
  resource_group_name = var.resource_group_name
  sku                 = "PerGB2018"
  retention_in_days   = var.log_retention_days
  tags                = var.tags
}

resource "azurerm_application_insights" "vigil" {
  name                = "appi-uks-vigil-01"
  location            = var.location
  resource_group_name = var.resource_group_name
  workspace_id        = azurerm_log_analytics_workspace.vigil.id
  application_type    = "web"
  tags                = var.tags
}

resource "azurerm_consumption_budget_resource_group" "vigil" {
  name              = "budget-uks-vigil-01"
  resource_group_id = "/subscriptions/${data.azurerm_client_config.current.subscription_id}/resourceGroups/${var.resource_group_name}"

  amount     = var.monthly_budget_gbp
  time_grain = "Monthly"

  time_period {
    # Fixed date — using timestamp() here causes a perpetual diff on every terraform plan
    start_date = "2026-03-01T00:00:00Z"
  }

  notification {
    enabled        = true
    threshold      = 80
    operator       = "GreaterThan"
    threshold_type = "Actual"
    contact_emails = [var.budget_alert_email]
  }

  notification {
    enabled        = true
    threshold      = 100
    operator       = "GreaterThan"
    threshold_type = "Actual"
    contact_emails = [var.budget_alert_email]
  }
}

data "azurerm_client_config" "current" {}
```

- [ ] **Step 3: Write `modules/monitoring/outputs.tf`**

```hcl
output "law_workspace_id" {
  value       = azurerm_log_analytics_workspace.vigil.id
  description = "Log Analytics workspace ID — passed to Container Apps environment"
}

output "app_insights_connection_string" {
  value       = azurerm_application_insights.vigil.connection_string
  sensitive   = true
  description = "App Insights connection string — injected as env var in all Container Apps"
}
```

- [ ] **Step 4: Add monitoring module call to `main.tf`**

Append to `infrastructure/terraform/main.tf`:

```hcl
module "monitoring" {
  source = "./modules/monitoring"

  resource_group_name = azurerm_resource_group.vigil.name
  location            = var.location
  log_retention_days  = var.log_retention_days
  monthly_budget_gbp  = var.monthly_budget_gbp
  budget_alert_email  = var.budget_alert_email
  tags                = local.tags
}
```

- [ ] **Step 5: Validate**

```bash
cd infrastructure/terraform
terraform validate
```

Expected: `Success! The configuration is valid.`

- [ ] **Step 6: Commit**

```bash
git add infrastructure/terraform/modules/monitoring/ infrastructure/terraform/main.tf
git commit -m "feat(terraform): add monitoring module (Log Analytics, App Insights, cost alert)"
```

---

### Task 4: Networking Module — VNets, Subnets, NSGs, DNS Zones

**Files:**
- Create: `infrastructure/terraform/modules/networking/variables.tf`
- Create: `infrastructure/terraform/modules/networking/main.tf`
- Create: `infrastructure/terraform/modules/networking/outputs.tf`
- Modify: `infrastructure/terraform/main.tf`

- [ ] **Step 1: Write `modules/networking/variables.tf`**

```hcl
variable "resource_group_name" {
  type        = string
  description = "Name of the Azure resource group"
}

variable "location" {
  type        = string
  description = "Azure region"
}

variable "tags" {
  type        = map(string)
  description = "Tags to apply to all resources"
  default     = {}
}
```

- [ ] **Step 2: Write `modules/networking/main.tf`**

```hcl
# ── Hub VNet ────────────────────────────────────────────────────────────────
resource "azurerm_virtual_network" "hub" {
  name                = "vnet-uks-hub-01"
  location            = var.location
  resource_group_name = var.resource_group_name
  address_space       = ["10.0.0.0/16"]
  tags                = var.tags
}

resource "azurerm_subnet" "gateway" {
  name                 = "GatewaySubnet"
  resource_group_name  = var.resource_group_name
  virtual_network_name = azurerm_virtual_network.hub.name
  address_prefixes     = ["10.0.0.0/27"]
}

resource "azurerm_subnet" "mgmt" {
  name                 = "snet-uks-mgmt-01"
  resource_group_name  = var.resource_group_name
  virtual_network_name = azurerm_virtual_network.hub.name
  address_prefixes     = ["10.0.1.0/24"]
}

# ── Spoke VNet ───────────────────────────────────────────────────────────────
resource "azurerm_virtual_network" "spoke" {
  name                = "vnet-uks-vigil-01"
  location            = var.location
  resource_group_name = var.resource_group_name
  address_space       = ["10.1.0.0/16"]
  tags                = var.tags
}

# /21 is the minimum required for a Dedicated workload profile on Container Apps
resource "azurerm_subnet" "cae" {
  name                 = "snet-uks-cae-01"
  resource_group_name  = var.resource_group_name
  virtual_network_name = azurerm_virtual_network.spoke.name
  address_prefixes     = ["10.1.0.0/21"]
  delegation {
    name = "cae-delegation"
    service_delegation {
      name = "Microsoft.App/environments"
      actions = [
        "Microsoft.Network/virtualNetworks/subnets/action",
      ]
    }
  }
}

resource "azurerm_subnet" "pe" {
  name                                          = "snet-uks-pe-01"
  resource_group_name                           = var.resource_group_name
  virtual_network_name                          = azurerm_virtual_network.spoke.name
  address_prefixes                              = ["10.1.8.0/24"]
  private_endpoint_network_policies_enabled     = false
}

# ── NSGs ─────────────────────────────────────────────────────────────────────
resource "azurerm_network_security_group" "cae" {
  name                = "nsg-uks-cae-01"
  location            = var.location
  resource_group_name = var.resource_group_name

  # Deny inbound from internet; allow inbound within subnet (inter-service traffic)
  security_rule {
    name                       = "DenyInternetInbound"
    priority                   = 4000
    direction                  = "Inbound"
    access                     = "Deny"
    protocol                   = "*"
    source_port_range          = "*"
    destination_port_range     = "*"
    source_address_prefix      = "Internet"
    destination_address_prefix = "*"
  }

  security_rule {
    name                       = "AllowSubnetInbound"
    priority                   = 100
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "*"
    source_port_range          = "*"
    destination_port_range     = "*"
    source_address_prefix      = "10.1.0.0/21"
    destination_address_prefix = "10.1.0.0/21"
  }

  tags = var.tags
}

resource "azurerm_subnet_network_security_group_association" "cae" {
  subnet_id                 = azurerm_subnet.cae.id
  network_security_group_id = azurerm_network_security_group.cae.id
}

resource "azurerm_network_security_group" "pe" {
  name                = "nsg-uks-pe-01"
  location            = var.location
  resource_group_name = var.resource_group_name

  security_rule {
    name                       = "DenyInternetInbound"
    priority                   = 4000
    direction                  = "Inbound"
    access                     = "Deny"
    protocol                   = "*"
    source_port_range          = "*"
    destination_port_range     = "*"
    source_address_prefix      = "Internet"
    destination_address_prefix = "*"
  }

  tags = var.tags
}

resource "azurerm_subnet_network_security_group_association" "pe" {
  subnet_id                 = azurerm_subnet.pe.id
  network_security_group_id = azurerm_network_security_group.pe.id
}

# ── VNet Peering ─────────────────────────────────────────────────────────────
resource "azurerm_virtual_network_peering" "hub_to_spoke" {
  name                      = "peer-hub-to-spoke"
  resource_group_name       = var.resource_group_name
  virtual_network_name      = azurerm_virtual_network.hub.name
  remote_virtual_network_id = azurerm_virtual_network.spoke.id
  allow_forwarded_traffic   = true
}

resource "azurerm_virtual_network_peering" "spoke_to_hub" {
  name                      = "peer-spoke-to-hub"
  resource_group_name       = var.resource_group_name
  virtual_network_name      = azurerm_virtual_network.spoke.name
  remote_virtual_network_id = azurerm_virtual_network.hub.id
  allow_forwarded_traffic   = true
}

# ── Private DNS Zones ────────────────────────────────────────────────────────
locals {
  private_dns_zones = {
    cosmos    = "privatelink.documents.azure.com"
    keyvault  = "privatelink.vaultcore.azure.net"
    acr       = "privatelink.azurecr.io"
    search    = "privatelink.search.windows.net"
    aifoundry = "privatelink.cognitiveservices.azure.com"
  }
}

resource "azurerm_private_dns_zone" "zones" {
  for_each            = local.private_dns_zones
  name                = each.value
  resource_group_name = var.resource_group_name
  tags                = var.tags
}

resource "azurerm_private_dns_zone_virtual_network_link" "spoke" {
  for_each              = local.private_dns_zones
  name                  = "link-${each.key}-spoke"
  resource_group_name   = var.resource_group_name
  private_dns_zone_name = azurerm_private_dns_zone.zones[each.key].name
  virtual_network_id    = azurerm_virtual_network.spoke.id
  tags                  = var.tags
}
```

- [ ] **Step 3: Write `modules/networking/outputs.tf`**

```hcl
output "spoke_vnet_id" {
  value       = azurerm_virtual_network.spoke.id
  description = "Spoke VNet ID"
}

output "cae_subnet_id" {
  value       = azurerm_subnet.cae.id
  description = "Container Apps environment subnet ID"
}

output "pe_subnet_id" {
  value       = azurerm_subnet.pe.id
  description = "Private endpoints subnet ID — passed to all PaaS modules"
}

output "private_dns_zone_ids" {
  value       = { for k, v in azurerm_private_dns_zone.zones : k => v.id }
  description = "Map of service key to private DNS zone ID (cosmos, keyvault, acr, search, aifoundry)"
}
```

- [ ] **Step 4: Add networking module call to `main.tf`**

Append to `infrastructure/terraform/main.tf`:

```hcl
module "networking" {
  source = "./modules/networking"

  resource_group_name = azurerm_resource_group.vigil.name
  location            = var.location
  tags                = local.tags
}
```

- [ ] **Step 5: Validate**

```bash
cd infrastructure/terraform
terraform validate
```

Expected: `Success! The configuration is valid.`

- [ ] **Step 6: Commit**

```bash
git add infrastructure/terraform/modules/networking/ infrastructure/terraform/main.tf
git commit -m "feat(terraform): add networking module (hub-spoke VNets, NSGs, private DNS zones)"
```

---

### Task 5: Cosmos DB Module — Extend Existing Stubs

The existing `modules/cosmos-db/main.tf` already has `step_up_requests` and `step_up_grants` containers. **Do not duplicate them.** This task prepends the account, database, and four remaining containers, then adds the private endpoint.

**Files:**
- Modify: `infrastructure/terraform/modules/cosmos-db/main.tf`
- Modify: `infrastructure/terraform/modules/cosmos-db/variables.tf`
- Modify: `infrastructure/terraform/modules/cosmos-db/outputs.tf`

- [ ] **Step 1: Add `pe_subnet_id` and `private_dns_zone_id` to `modules/cosmos-db/variables.tf`**

Append to the existing file (keep existing variables):

```hcl
variable "pe_subnet_id" {
  type        = string
  description = "Private endpoint subnet ID"
}

variable "private_dns_zone_id" {
  type        = string
  description = "Private DNS zone ID for privatelink.documents.azure.com"
}

variable "location" {
  type        = string
  description = "Azure region"
}
```

- [ ] **Step 2: Prepend account, database, and four containers to `modules/cosmos-db/main.tf`**

Insert BEFORE the existing `azurerm_cosmosdb_sql_container.step_up_requests` resource:

```hcl
resource "azurerm_cosmosdb_account" "vigil" {
  name                = "cosmos-uks-vigil-01"
  location            = var.location
  resource_group_name = var.resource_group_name
  offer_type          = "Standard"
  kind                = "GlobalDocumentDB"

  capabilities {
    name = "EnableServerless"
  }

  consistency_policy {
    consistency_level = "Session"
  }

  geo_location {
    location          = var.location
    failover_priority = 0
  }

  backup {
    type = "Continuous"
    tier = "Continuous7Days"
  }

  public_network_access_enabled = false

  tags = var.tags
}

resource "azurerm_cosmosdb_sql_database" "vigil" {
  name                = var.cosmos_database_name
  resource_group_name = var.resource_group_name
  account_name        = azurerm_cosmosdb_account.vigil.name
}

resource "azurerm_cosmosdb_sql_container" "conversations" {
  name                  = "conversations"
  resource_group_name   = var.resource_group_name
  account_name          = azurerm_cosmosdb_account.vigil.name
  database_name         = azurerm_cosmosdb_sql_database.vigil.name
  partition_key_path    = "/tenant_id"
  partition_key_version = 1

  indexing_policy {
    indexing_mode = "consistent"
    included_path { path = "/*" }
  }

  tags = var.tags
}

resource "azurerm_cosmosdb_sql_container" "audit_logs" {
  name                  = "audit_logs"
  resource_group_name   = var.resource_group_name
  account_name          = azurerm_cosmosdb_account.vigil.name
  database_name         = azurerm_cosmosdb_sql_database.vigil.name
  partition_key_path    = "/tenant_id"
  partition_key_version = 1

  indexing_policy {
    indexing_mode = "consistent"
    included_path { path = "/*" }
  }

  tags = var.tags
}

resource "azurerm_cosmosdb_sql_container" "tenant_config" {
  name                  = "tenant_config"
  resource_group_name   = var.resource_group_name
  account_name          = azurerm_cosmosdb_account.vigil.name
  database_name         = azurerm_cosmosdb_sql_database.vigil.name
  partition_key_path    = "/tenant_id"
  partition_key_version = 1

  indexing_policy {
    indexing_mode = "consistent"
    included_path { path = "/*" }
  }

  tags = var.tags
}

resource "azurerm_cosmosdb_sql_container" "change_records" {
  name                  = "change_records"
  resource_group_name   = var.resource_group_name
  account_name          = azurerm_cosmosdb_account.vigil.name
  database_name         = azurerm_cosmosdb_sql_database.vigil.name
  partition_key_path    = "/tenant_id"
  partition_key_version = 1

  indexing_policy {
    indexing_mode = "consistent"
    included_path { path = "/*" }
  }

  tags = var.tags
}
```

The existing stubs use `var.cosmos_account_name` and `var.cosmos_database_name`. Update them to reference the new Terraform resources so Terraform can infer the dependency ordering. Replace the existing `step_up_requests` and `step_up_grants` resource blocks with:

```hcl
resource "azurerm_cosmosdb_sql_container" "step_up_requests" {
  name                  = "step_up_requests"
  resource_group_name   = var.resource_group_name
  account_name          = azurerm_cosmosdb_account.vigil.name   # was: var.cosmos_account_name
  database_name         = azurerm_cosmosdb_sql_database.vigil.name  # was: var.cosmos_database_name
  partition_key_path    = "/tenant_id"
  partition_key_version = 1

  indexing_policy {
    indexing_mode = "consistent"
    included_path { path = "/*" }
  }

  tags = var.tags
}

resource "azurerm_cosmosdb_sql_container" "step_up_grants" {
  name                  = "step_up_grants"
  resource_group_name   = var.resource_group_name
  account_name          = azurerm_cosmosdb_account.vigil.name   # was: var.cosmos_account_name
  database_name         = azurerm_cosmosdb_sql_database.vigil.name  # was: var.cosmos_database_name
  partition_key_path    = "/tenant_id"
  partition_key_version = 1

  # REQUIRED: default_ttl = -1 enables per-document TTL (_ttl field on each document).
  # Without this, the _ttl field is silently ignored and grants never expire in Cosmos DB.
  default_ttl = -1

  indexing_policy {
    indexing_mode = "consistent"
    included_path { path = "/*" }
  }

  tags = var.tags
}
```

Append the private endpoint AFTER the existing step_up containers:

```hcl
resource "azurerm_private_endpoint" "cosmos" {
  name                = "pe-uks-cosmos-01"
  location            = var.location
  resource_group_name = var.resource_group_name
  subnet_id           = var.pe_subnet_id

  private_service_connection {
    name                           = "cosmos-psc"
    private_connection_resource_id = azurerm_cosmosdb_account.vigil.id
    subresource_names              = ["Sql"]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "cosmos-dns-group"
    private_dns_zone_ids = [var.private_dns_zone_id]
  }

  tags = var.tags
}
```

- [ ] **Step 3: Append new outputs to `modules/cosmos-db/outputs.tf`**

Append (keep existing outputs):

```hcl
output "cosmos_endpoint" {
  value       = azurerm_cosmosdb_account.vigil.endpoint
  description = "Cosmos DB account endpoint URL"
}

output "cosmos_account_name" {
  value       = azurerm_cosmosdb_account.vigil.name
  description = "Cosmos DB account name — used for RBAC scope"
}

output "cosmos_account_id" {
  value       = azurerm_cosmosdb_account.vigil.id
  description = "Cosmos DB account resource ID — used for RBAC role definition scope"
}
```

- [ ] **Step 4: Add cosmos-db module call to `main.tf`**

Append to `infrastructure/terraform/main.tf`:

```hcl
module "cosmos_db" {
  source = "./modules/cosmos-db"

  resource_group_name = azurerm_resource_group.vigil.name
  location            = var.location
  cosmos_account_name = "cosmos-uks-vigil-01"
  cosmos_database_name = var.cosmos_database_name
  pe_subnet_id        = module.networking.pe_subnet_id
  private_dns_zone_id = module.networking.private_dns_zone_ids["cosmos"]
  tags                = local.tags
}
```

- [ ] **Step 5: Validate**

```bash
cd infrastructure/terraform
terraform validate
```

Expected: `Success! The configuration is valid.`

- [ ] **Step 6: Commit**

```bash
git add infrastructure/terraform/modules/cosmos-db/ infrastructure/terraform/main.tf
git commit -m "feat(terraform): extend cosmos-db module with account, database, containers, and PE"
```

---

### Task 6: Key Vault Module

**Files:**
- Create: `infrastructure/terraform/modules/key-vault/variables.tf`
- Create: `infrastructure/terraform/modules/key-vault/main.tf`
- Create: `infrastructure/terraform/modules/key-vault/outputs.tf`
- Modify: `infrastructure/terraform/main.tf`

- [ ] **Step 1: Write `modules/key-vault/variables.tf`**

```hcl
variable "resource_group_name" {
  type        = string
  description = "Name of the Azure resource group"
}

variable "location" {
  type        = string
  description = "Azure region"
}

variable "pe_subnet_id" {
  type        = string
  description = "Private endpoint subnet ID"
}

variable "private_dns_zone_id" {
  type        = string
  description = "Private DNS zone ID for privatelink.vaultcore.azure.net"
}

variable "tags" {
  type        = map(string)
  description = "Tags to apply to all resources"
  default     = {}
}
```

- [ ] **Step 2: Write `modules/key-vault/main.tf`**

```hcl
data "azurerm_client_config" "current" {}

resource "azurerm_key_vault" "vigil" {
  name                = "kv-uks-vigil-01"
  location            = var.location
  resource_group_name = var.resource_group_name
  tenant_id           = data.azurerm_client_config.current.tenant_id
  sku_name            = "standard"

  soft_delete_retention_days  = 90
  purge_protection_enabled    = true
  enable_rbac_authorization   = true
  public_network_access_enabled = false

  tags = var.tags
}

resource "azurerm_private_endpoint" "keyvault" {
  name                = "pe-uks-kv-01"
  location            = var.location
  resource_group_name = var.resource_group_name
  subnet_id           = var.pe_subnet_id

  private_service_connection {
    name                           = "kv-psc"
    private_connection_resource_id = azurerm_key_vault.vigil.id
    subresource_names              = ["vault"]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "kv-dns-group"
    private_dns_zone_ids = [var.private_dns_zone_id]
  }

  tags = var.tags
}
```

- [ ] **Step 3: Write `modules/key-vault/outputs.tf`**

```hcl
output "key_vault_uri" {
  value       = azurerm_key_vault.vigil.vault_uri
  description = "Key Vault URI — injected as KEY_VAULT_URL env var in all Container Apps"
}

output "key_vault_id" {
  value       = azurerm_key_vault.vigil.id
  description = "Key Vault resource ID — used for RBAC scope"
}
```

- [ ] **Step 4: Add key-vault module call to `main.tf`**

Append to `infrastructure/terraform/main.tf`:

```hcl
module "key_vault" {
  source = "./modules/key-vault"

  resource_group_name = azurerm_resource_group.vigil.name
  location            = var.location
  pe_subnet_id        = module.networking.pe_subnet_id
  private_dns_zone_id = module.networking.private_dns_zone_ids["keyvault"]
  tags                = local.tags
}
```

- [ ] **Step 5: Validate**

```bash
cd infrastructure/terraform
terraform validate
```

Expected: `Success! The configuration is valid.`

- [ ] **Step 6: Commit**

```bash
git add infrastructure/terraform/modules/key-vault/ infrastructure/terraform/main.tf
git commit -m "feat(terraform): add key-vault module"
```

---

### Task 7: ACR Module

**Files:**
- Create: `infrastructure/terraform/modules/acr/variables.tf`
- Create: `infrastructure/terraform/modules/acr/main.tf`
- Create: `infrastructure/terraform/modules/acr/outputs.tf`
- Modify: `infrastructure/terraform/main.tf`

- [ ] **Step 1: Write `modules/acr/variables.tf`**

```hcl
variable "resource_group_name" {
  type        = string
  description = "Name of the Azure resource group"
}

variable "location" {
  type        = string
  description = "Azure region"
}

variable "acr_name" {
  type        = string
  description = "Name of the container registry (alphanumeric only, no hyphens)"
}

variable "pe_subnet_id" {
  type        = string
  description = "Private endpoint subnet ID"
}

variable "private_dns_zone_id" {
  type        = string
  description = "Private DNS zone ID for privatelink.azurecr.io"
}

variable "tags" {
  type        = map(string)
  description = "Tags to apply to all resources"
  default     = {}
}
```

- [ ] **Step 2: Write `modules/acr/main.tf`**

```hcl
resource "azurerm_container_registry" "vigil" {
  name                = var.acr_name
  resource_group_name = var.resource_group_name
  location            = var.location
  # Premium is required for private endpoint support — Basic and Standard do not support it
  sku                           = "Premium"
  admin_enabled                 = false
  public_network_access_enabled = false
  tags                          = var.tags
}

resource "azurerm_private_endpoint" "acr" {
  name                = "pe-uks-acr-01"
  location            = var.location
  resource_group_name = var.resource_group_name
  subnet_id           = var.pe_subnet_id

  private_service_connection {
    name                           = "acr-psc"
    private_connection_resource_id = azurerm_container_registry.vigil.id
    subresource_names              = ["registry"]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "acr-dns-group"
    private_dns_zone_ids = [var.private_dns_zone_id]
  }

  tags = var.tags
}
```

- [ ] **Step 3: Write `modules/acr/outputs.tf`**

```hcl
output "login_server" {
  value       = azurerm_container_registry.vigil.login_server
  description = "ACR login server FQDN — used as registry for all Container Apps"
}

output "registry_id" {
  value       = azurerm_container_registry.vigil.id
  description = "ACR resource ID — used for AcrPull and AcrPush RBAC scope"
}
```

- [ ] **Step 4: Add ACR module call to `main.tf`**

Append to `infrastructure/terraform/main.tf`:

```hcl
module "acr" {
  source = "./modules/acr"

  resource_group_name = azurerm_resource_group.vigil.name
  location            = var.location
  acr_name            = var.acr_name
  pe_subnet_id        = module.networking.pe_subnet_id
  private_dns_zone_id = module.networking.private_dns_zone_ids["acr"]
  tags                = local.tags
}
```

- [ ] **Step 5: Validate**

```bash
cd infrastructure/terraform
terraform validate
```

Expected: `Success! The configuration is valid.`

- [ ] **Step 6: Commit**

```bash
git add infrastructure/terraform/modules/acr/ infrastructure/terraform/main.tf
git commit -m "feat(terraform): add ACR module (Premium SKU, private endpoint)"
```

---

### Task 8: AI Search Module

**Files:**
- Create: `infrastructure/terraform/modules/ai-search/variables.tf`
- Create: `infrastructure/terraform/modules/ai-search/main.tf`
- Create: `infrastructure/terraform/modules/ai-search/outputs.tf`
- Modify: `infrastructure/terraform/main.tf`

- [ ] **Step 1: Write `modules/ai-search/variables.tf`**

```hcl
variable "resource_group_name" {
  type        = string
  description = "Name of the Azure resource group"
}

variable "location" {
  type        = string
  description = "Azure region"
}

variable "pe_subnet_id" {
  type        = string
  description = "Private endpoint subnet ID"
}

variable "private_dns_zone_id" {
  type        = string
  description = "Private DNS zone ID for privatelink.search.windows.net"
}

variable "tags" {
  type        = map(string)
  description = "Tags to apply to all resources"
  default     = {}
}
```

- [ ] **Step 2: Write `modules/ai-search/main.tf`**

```hcl
resource "azurerm_search_service" "vigil" {
  name                = "srch-uks-vigil-01"
  resource_group_name = var.resource_group_name
  location            = var.location
  sku                 = "basic"
  replica_count       = 1
  partition_count     = 1
  public_network_access_enabled = false
  local_authentication_enabled  = false
  tags                = var.tags
}

resource "azurerm_private_endpoint" "search" {
  name                = "pe-uks-search-01"
  location            = var.location
  resource_group_name = var.resource_group_name
  subnet_id           = var.pe_subnet_id

  private_service_connection {
    name                           = "search-psc"
    private_connection_resource_id = azurerm_search_service.vigil.id
    subresource_names              = ["searchService"]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "search-dns-group"
    private_dns_zone_ids = [var.private_dns_zone_id]
  }

  tags = var.tags
}
```

Note: The `vigil-knowledge` index schema (field definitions beyond `tenant_id`) is populated during RAG knowledge base setup — not during initial infrastructure provision. The search service and PE are sufficient for first apply.

- [ ] **Step 3: Write `modules/ai-search/outputs.tf`**

```hcl
output "search_endpoint" {
  value       = "https://${azurerm_search_service.vigil.name}.search.windows.net"
  description = "AI Search service endpoint — injected as AI_SEARCH_ENDPOINT env var"
}

output "search_service_name" {
  value       = azurerm_search_service.vigil.name
  description = "AI Search service name — used for RBAC scope"
}

output "search_service_id" {
  value       = azurerm_search_service.vigil.id
  description = "AI Search resource ID — used for RBAC scope"
}
```

- [ ] **Step 4: Add AI Search module call to `main.tf`**

Append to `infrastructure/terraform/main.tf`:

```hcl
module "ai_search" {
  source = "./modules/ai-search"

  resource_group_name = azurerm_resource_group.vigil.name
  location            = var.location
  pe_subnet_id        = module.networking.pe_subnet_id
  private_dns_zone_id = module.networking.private_dns_zone_ids["search"]
  tags                = local.tags
}
```

- [ ] **Step 5: Validate**

```bash
cd infrastructure/terraform
terraform validate
```

Expected: `Success! The configuration is valid.`

- [ ] **Step 6: Commit**

```bash
git add infrastructure/terraform/modules/ai-search/ infrastructure/terraform/main.tf
git commit -m "feat(terraform): add AI Search module"
```

---

### Task 9: AI Foundry Module

**Files:**
- Create: `infrastructure/terraform/modules/ai-foundry/variables.tf`
- Create: `infrastructure/terraform/modules/ai-foundry/main.tf`
- Create: `infrastructure/terraform/modules/ai-foundry/outputs.tf`
- Modify: `infrastructure/terraform/main.tf`

**Before applying:** Verify Claude Sonnet 4.6 is available in UK South at [https://learn.microsoft.com/en-us/azure/ai-services/openai/concepts/models](). If unavailable, change `location` to `swedencentral` for this module only (note the model deployment requires the account to be in a supported region — the private endpoint DNS record will still resolve correctly).

- [ ] **Step 1: Write `modules/ai-foundry/variables.tf`**

```hcl
variable "resource_group_name" {
  type        = string
  description = "Name of the Azure resource group"
}

variable "location" {
  type        = string
  description = "Azure region. If Claude Sonnet 4.6 is unavailable here, override to swedencentral."
}

variable "model_tpm" {
  type        = number
  description = "Tokens-per-minute quota for the Claude Sonnet 4.6 model deployment"
}

variable "pe_subnet_id" {
  type        = string
  description = "Private endpoint subnet ID"
}

variable "private_dns_zone_id" {
  type        = string
  description = "Private DNS zone ID for privatelink.cognitiveservices.azure.com"
}

variable "tags" {
  type        = map(string)
  description = "Tags to apply to all resources"
  default     = {}
}
```

- [ ] **Step 2: Write `modules/ai-foundry/main.tf`**

```hcl
resource "azurerm_cognitive_account" "vigil" {
  name                = "aif-uks-vigil-01"
  location            = var.location
  resource_group_name = var.resource_group_name
  kind                = "AIServices"
  sku_name            = "S0"

  public_network_access_enabled = false

  tags = var.tags
}

resource "azurerm_cognitive_deployment" "claude" {
  name                 = "claude-sonnet-4-6"
  cognitive_account_id = azurerm_cognitive_account.vigil.id

  model {
    format  = "Anthropic"
    name    = "claude-sonnet-4-6"
    version = "latest"
  }

  scale {
    type     = "Standard"
    capacity = var.model_tpm
  }
}

resource "azurerm_private_endpoint" "aifoundry" {
  name                = "pe-uks-aif-01"
  location            = var.location
  resource_group_name = var.resource_group_name
  subnet_id           = var.pe_subnet_id

  private_service_connection {
    name                           = "aifoundry-psc"
    private_connection_resource_id = azurerm_cognitive_account.vigil.id
    subresource_names              = ["account"]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "aifoundry-dns-group"
    private_dns_zone_ids = [var.private_dns_zone_id]
  }

  tags = var.tags
}
```

- [ ] **Step 3: Write `modules/ai-foundry/outputs.tf`**

```hcl
output "ai_foundry_endpoint" {
  value       = azurerm_cognitive_account.vigil.endpoint
  description = "AI Foundry endpoint URL — injected as AI_FOUNDRY_ENDPOINT env var"
}

output "ai_foundry_id" {
  value       = azurerm_cognitive_account.vigil.id
  description = "AI Foundry resource ID — used for RBAC scope"
}
```

- [ ] **Step 4: Add AI Foundry module call to `main.tf`**

Append to `infrastructure/terraform/main.tf`:

```hcl
module "ai_foundry" {
  source = "./modules/ai-foundry"

  resource_group_name = azurerm_resource_group.vigil.name
  location            = var.location
  model_tpm           = var.ai_foundry_model_tpm
  pe_subnet_id        = module.networking.pe_subnet_id
  private_dns_zone_id = module.networking.private_dns_zone_ids["aifoundry"]
  tags                = local.tags
}
```

- [ ] **Step 5: Validate**

```bash
cd infrastructure/terraform
terraform validate
```

Expected: `Success! The configuration is valid.`

- [ ] **Step 6: Commit**

```bash
git add infrastructure/terraform/modules/ai-foundry/ infrastructure/terraform/main.tf
git commit -m "feat(terraform): add AI Foundry module (Claude Sonnet 4.6 deployment)"
```

---

### Task 10: Container Apps Module

The existing stub has a comment in `main.tf` and one variable (`ingress_timeout_seconds`). This task adds all Container Apps resources while preserving the existing comment.

**Files:**
- Modify: `infrastructure/terraform/modules/container-apps/main.tf`
- Modify: `infrastructure/terraform/modules/container-apps/variables.tf`
- Create: `infrastructure/terraform/modules/container-apps/outputs.tf`
- Modify: `infrastructure/terraform/main.tf`

- [ ] **Step 1: Replace `modules/container-apps/variables.tf` (add all inputs, keep existing `ingress_timeout_seconds`)**

```hcl
variable "resource_group_name" {
  type        = string
  description = "Name of the Azure resource group"
}

variable "location" {
  type        = string
  description = "Azure region"
}

variable "cae_subnet_id" {
  type        = string
  description = "Container Apps environment subnet ID"
}

variable "law_workspace_id" {
  type        = string
  description = "Log Analytics workspace ID for CAE diagnostic settings"
}

variable "acr_login_server" {
  type        = string
  description = "ACR login server FQDN — set as registry for all apps"
}

variable "key_vault_url" {
  type        = string
  description = "Key Vault URI — injected as KEY_VAULT_URL"
}

variable "cosmos_endpoint" {
  type        = string
  description = "Cosmos DB account endpoint — injected as COSMOS_ENDPOINT"
}

variable "ai_foundry_endpoint" {
  type        = string
  description = "AI Foundry endpoint URL — injected as AI_FOUNDRY_ENDPOINT"
}

variable "ai_search_endpoint" {
  type        = string
  description = "AI Search endpoint URL — injected as AI_SEARCH_ENDPOINT"
}

variable "app_insights_connection_string" {
  type        = string
  sensitive   = true
  description = "App Insights connection string — injected as APPLICATIONINSIGHTS_CONNECTION_STRING"
}

variable "ingress_timeout_seconds" {
  type        = number
  description = "HTTP request timeout in seconds. Must exceed the longest step_up pending_ttl_seconds + 60."
  default     = 960 # 900s (15min TTL) + 60s buffer
}

variable "tags" {
  type        = map(string)
  description = "Tags to apply to all resources"
  default     = {}
}
```

- [ ] **Step 2: Append Container Apps resources to `modules/container-apps/main.tf`**

Keep the existing comment. Append all resources after it:

```hcl
resource "azurerm_container_app_environment" "vigil" {
  name                           = "cae-uks-vigil-01"
  location                       = var.location
  resource_group_name            = var.resource_group_name
  log_analytics_workspace_id     = var.law_workspace_id
  infrastructure_subnet_id       = var.cae_subnet_id
  internal_load_balancer_enabled = true

  workload_profile {
    name                  = "Consumption"
    workload_profile_type = "Consumption"
  }

  # Dedicated-D4 is required for ca-uks-agent-probe-01 (NET_ADMIN + NET_RAW capabilities).
  # NET_ADMIN/NET_RAW are not available on Consumption workload profiles.
  # NOTE: the NET_ADMIN and NET_RAW capabilities themselves are NOT configurable via the
  # azurerm Terraform provider. They are applied via az containerapp update in
  # deploy-agent-probe.yml as a post-deploy step. See "Known Terraform Provider Gaps" in
  # the infrastructure design doc.
  workload_profile {
    name                  = "Dedicated-D4"
    workload_profile_type = "D4"
    minimum_count         = 1
    maximum_count         = 3
  }

  tags = var.tags
}

locals {
  # All 11 Container Apps. min/max replicas and ingress type differ per app.
  container_apps = {
    "ca-uks-gateway-01" = {
      workload_profile_name = "Consumption"
      external_enabled      = true
      min_replicas          = 1
      max_replicas          = 5
    }
    "ca-uks-ui-01" = {
      workload_profile_name = "Consumption"
      external_enabled      = true
      min_replicas          = 1
      max_replicas          = 5
    }
    "ca-uks-coordinator-01" = {
      workload_profile_name = "Consumption"
      external_enabled      = false
      min_replicas          = 1
      max_replicas          = 5
    }
    "ca-uks-agent-network-01" = {
      workload_profile_name = "Consumption"
      external_enabled      = false
      min_replicas          = 1
      max_replicas          = 5
    }
    "ca-uks-agent-rag-01" = {
      workload_profile_name = "Consumption"
      external_enabled      = false
      min_replicas          = 1
      max_replicas          = 5
    }
    "ca-uks-agent-itsm-01" = {
      workload_profile_name = "Consumption"
      external_enabled      = false
      min_replicas          = 1
      max_replicas          = 5
    }
    "ca-uks-agent-enrichment-01" = {
      workload_profile_name = "Consumption"
      external_enabled      = false
      min_replicas          = 1
      max_replicas          = 5
    }
    "ca-uks-agent-change-reviewer-01" = {
      workload_profile_name = "Consumption"
      external_enabled      = false
      min_replicas          = 1
      max_replicas          = 5
    }
    "ca-uks-agent-design-01" = {
      workload_profile_name = "Consumption"
      external_enabled      = false
      min_replicas          = 1
      max_replicas          = 5
    }
    "ca-uks-agent-troubleshoot-01" = {
      workload_profile_name = "Consumption"
      external_enabled      = false
      min_replicas          = 1
      max_replicas          = 5
    }
    "ca-uks-agent-probe-01" = {
      workload_profile_name = "Dedicated-D4"
      external_enabled      = false
      min_replicas          = 1
      max_replicas          = 3
    }
  }
}

resource "azurerm_container_app" "apps" {
  for_each                     = local.container_apps
  name                         = each.key
  container_app_environment_id = azurerm_container_app_environment.vigil.id
  resource_group_name          = var.resource_group_name
  revision_mode                = "Single"
  workload_profile_name        = each.value.workload_profile_name

  identity {
    type = "SystemAssigned"
  }

  template {
    min_replicas = each.value.min_replicas
    max_replicas = each.value.max_replicas

    container {
      name   = each.key
      image  = "mcr.microsoft.com/azuredocs/containerapps-helloworld:latest"
      cpu    = 0.25
      memory = "0.5Gi"

      env {
        name  = "COSMOS_ENDPOINT"
        value = var.cosmos_endpoint
      }
      env {
        name  = "KEY_VAULT_URL"
        value = var.key_vault_url
      }
      env {
        name  = "AI_FOUNDRY_ENDPOINT"
        value = var.ai_foundry_endpoint
      }
      env {
        name  = "AI_SEARCH_ENDPOINT"
        value = var.ai_search_endpoint
      }
      env {
        name  = "APPLICATIONINSIGHTS_CONNECTION_STRING"
        value = var.app_insights_connection_string
      }
      env {
        name  = "COORDINATOR_URL"
        value = "https://ca-uks-coordinator-01.${azurerm_container_app_environment.vigil.default_domain}"
      }
    }
  }

  ingress {
    external_enabled = each.value.external_enabled
    target_port      = 8000
    transport        = "http"

    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }

  tags = var.tags
}
```

- [ ] **Step 3: Write `modules/container-apps/outputs.tf`**

```hcl
output "app_fqdns" {
  value       = { for k, v in azurerm_container_app.apps : k => v.latest_revision_fqdn }
  description = "Map of Container App name to FQDN — used for inter-service env vars and RBAC wiring"
}

output "app_identity_principal_ids" {
  value       = { for k, v in azurerm_container_app.apps : k => v.identity[0].principal_id }
  description = "Map of Container App name to Managed Identity principal ID — used for RBAC assignments"
}
```

- [ ] **Step 4: Add container-apps module call to `main.tf`**

Append to `infrastructure/terraform/main.tf`:

```hcl
module "container_apps" {
  source = "./modules/container-apps"

  resource_group_name            = azurerm_resource_group.vigil.name
  location                       = var.location
  cae_subnet_id                  = module.networking.cae_subnet_id
  law_workspace_id               = module.monitoring.law_workspace_id
  acr_login_server               = module.acr.login_server
  key_vault_url                  = module.key_vault.key_vault_uri
  cosmos_endpoint                = module.cosmos_db.cosmos_endpoint
  ai_foundry_endpoint            = module.ai_foundry.ai_foundry_endpoint
  ai_search_endpoint             = module.ai_search.search_endpoint
  app_insights_connection_string = module.monitoring.app_insights_connection_string
  tags                           = local.tags
}
```

- [ ] **Step 5: Validate**

```bash
cd infrastructure/terraform
terraform validate
```

Expected: `Success! The configuration is valid.`

- [ ] **Step 6: Commit**

```bash
git add infrastructure/terraform/modules/container-apps/ infrastructure/terraform/main.tf
git commit -m "feat(terraform): add container-apps module (CAE, workload profiles, 11 Container Apps)"
```

---

### Task 11: RBAC Role Assignments and GitHub Actions Service Principal

All RBAC resources go in root `main.tf`. This task appends them after the module calls.

**Files:**
- Modify: `infrastructure/terraform/main.tf`
- Modify: `infrastructure/terraform/outputs.tf`

- [ ] **Step 1: Append RBAC and GitHub Actions SP to `main.tf`**

Append to `infrastructure/terraform/main.tf`:

```hcl
# ── Data sources for built-in role definitions ──────────────────────────────
data "azurerm_role_definition" "kv_secrets_user" {
  name = "Key Vault Secrets User"
}

data "azurerm_role_definition" "search_index_contributor" {
  name = "Search Index Data Contributor"
}

data "azurerm_role_definition" "cognitive_openai_user" {
  name = "Cognitive Services OpenAI User"
}

data "azurerm_role_definition" "acr_pull" {
  name = "AcrPull"
}

data "azurerm_role_definition" "acr_push" {
  name = "AcrPush"
}

data "azurerm_role_definition" "container_apps_contributor" {
  name = "Azure Container Apps Contributor"
}

# ── Cosmos DB data plane RBAC (uses azurerm_cosmosdb_sql_role_assignment) ───
# Built-in role: "Cosmos DB Built-in Data Contributor" ID = 00000000-0000-0000-0000-000000000002
# Assigned to: gateway, coordinator, agent-network, agent-rag, agent-itsm,
#              agent-enrichment, agent-change-reviewer, agent-design, agent-troubleshoot
# Excluded: agent-probe (no tenant awareness, no audit writes — auditing handled by agent-troubleshoot)

locals {
  cosmos_data_contributor_apps = toset([
    "ca-uks-gateway-01",
    "ca-uks-coordinator-01",
    "ca-uks-agent-network-01",
    "ca-uks-agent-rag-01",
    "ca-uks-agent-itsm-01",
    "ca-uks-agent-enrichment-01",
    "ca-uks-agent-change-reviewer-01",
    "ca-uks-agent-design-01",
    "ca-uks-agent-troubleshoot-01",
  ])

  kv_secrets_user_apps = toset([
    "ca-uks-agent-network-01",     # ise-tacacs-key
    "ca-uks-agent-itsm-01",        # jira-api-token, jira-base-url
    "ca-uks-agent-enrichment-01",  # cisco-support-api-key, shodan-api-key
    "ca-uks-agent-troubleshoot-01", # tenant-{id}-* vendor credentials (JIT fetch)
    # coordinator excluded: accesses Cosmos DB and AI Foundry via Managed Identity only
  ])

  search_index_contributor_apps = toset([
    "ca-uks-agent-rag-01",
    "ca-uks-agent-design-01",
  ])

  cognitive_openai_user_apps = toset([
    "ca-uks-coordinator-01",
    "ca-uks-agent-change-reviewer-01",
    "ca-uks-agent-design-01",
    "ca-uks-agent-troubleshoot-01",
  ])
}

resource "azurerm_cosmosdb_sql_role_assignment" "cosmos_data_contributor" {
  for_each            = local.cosmos_data_contributor_apps
  resource_group_name = azurerm_resource_group.vigil.name
  account_name        = module.cosmos_db.cosmos_account_name
  role_definition_id  = "${module.cosmos_db.cosmos_account_id}/sqlRoleDefinitions/00000000-0000-0000-0000-000000000002"
  principal_id        = module.container_apps.app_identity_principal_ids[each.key]
  scope               = module.cosmos_db.cosmos_account_id
}

resource "azurerm_role_assignment" "kv_secrets_user" {
  for_each             = local.kv_secrets_user_apps
  scope                = module.key_vault.key_vault_id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = module.container_apps.app_identity_principal_ids[each.key]
}

resource "azurerm_role_assignment" "search_index_contributor" {
  for_each             = local.search_index_contributor_apps
  scope                = module.ai_search.search_service_id
  role_definition_name = "Search Index Data Contributor"
  principal_id         = module.container_apps.app_identity_principal_ids[each.key]
}

resource "azurerm_role_assignment" "cognitive_openai_user" {
  for_each             = local.cognitive_openai_user_apps
  scope                = module.ai_foundry.ai_foundry_id
  role_definition_name = "Cognitive Services OpenAI User"
  principal_id         = module.container_apps.app_identity_principal_ids[each.key]
}

# AcrPull — all 11 Container Apps
resource "azurerm_role_assignment" "acr_pull" {
  for_each             = module.container_apps.app_identity_principal_ids
  scope                = module.acr.registry_id
  role_definition_name = "AcrPull"
  principal_id         = each.value
}

# ── GitHub Actions Service Principal ────────────────────────────────────────
resource "azuread_application" "github_actions" {
  display_name = "sp-uks-github-vigil-01"
}

resource "azuread_service_principal" "github_actions" {
  client_id = azuread_application.github_actions.client_id
}

resource "azuread_application_password" "github_actions" {
  application_id = azuread_application.github_actions.id
}

resource "azurerm_role_assignment" "github_acr_push" {
  scope                = module.acr.registry_id
  role_definition_name = "AcrPush"
  principal_id         = azuread_service_principal.github_actions.object_id
}

resource "azurerm_role_assignment" "github_ca_contributor" {
  scope                = azurerm_resource_group.vigil.id
  role_definition_name = "Azure Container Apps Contributor"
  principal_id         = azuread_service_principal.github_actions.object_id
}
```

- [ ] **Step 2: Populate `infrastructure/terraform/outputs.tf`**

Replace the placeholder:

```hcl
output "coordinator_fqdn" {
  value       = module.container_apps.app_fqdns["ca-uks-coordinator-01"]
  description = "Internal FQDN of the coordinator Container App"
}

output "gateway_fqdn" {
  value       = module.container_apps.app_fqdns["ca-uks-gateway-01"]
  description = "External FQDN of the gateway Container App (public entry point)"
}

output "key_vault_uri" {
  value       = module.key_vault.key_vault_uri
  description = "Key Vault URI — used to manually create secrets post-apply"
}

output "acr_login_server" {
  value       = module.acr.login_server
  description = "ACR login server — set as REGISTRY_LOGIN_SERVER GitHub Actions secret"
}

output "app_insights_connection_string" {
  value     = module.monitoring.app_insights_connection_string
  sensitive = true
}

output "github_actions_client_id" {
  value       = azuread_application.github_actions.client_id
  description = "GitHub Actions SP client ID — set in AZURE_CREDENTIALS secret"
}

output "github_actions_client_secret" {
  value       = azuread_application_password.github_actions.value
  sensitive   = true
  description = "GitHub Actions SP client secret — set in AZURE_CREDENTIALS secret"
}
```

- [ ] **Step 3: Validate**

```bash
cd infrastructure/terraform
terraform validate
```

Expected: `Success! The configuration is valid.`

- [ ] **Step 4: Commit**

```bash
git add infrastructure/terraform/main.tf infrastructure/terraform/outputs.tf
git commit -m "feat(terraform): add RBAC role assignments and GitHub Actions service principal"
```

---

### Task 12: Validation Gate and Post-Apply Manual Steps

- [ ] **Step 1: Run full `terraform validate`**

```bash
cd infrastructure/terraform
terraform validate
```

Expected: `Success! The configuration is valid.`

If any errors, fix them before proceeding.

- [ ] **Step 2: Run `terraform plan` (requires Azure credentials)**

```bash
terraform plan -var-file=environments/prod.tfvars -out=tfplan
```

Expected: Plan lists resources to create, no errors. Review the plan output — check that:
- Resource group `rg-uks-vigil-01` is created
- 2 VNets, 4 subnets, 2 NSGs, 2 peerings created
- 5 private DNS zones created with VNet links
- Cosmos DB account (serverless) + database + 6 containers created
- Key Vault (standard, purge-protected) created
- ACR (Premium) created
- AI Search (basic) created
- AI Foundry account + claude-sonnet-4-6 deployment created
- Container Apps environment (internal, Dedicated-D4 + Consumption profiles) + 11 apps created
- RBAC assignments: 9 Cosmos, 4 KV, 2 search, 4 AI Foundry, 11 AcrPull created
- GitHub Actions SP + 2 role assignments created
- Private endpoints: 5 (one per PaaS service) created

- [ ] **Step 3: Apply**

```bash
terraform apply tfplan
```

Expected: `Apply complete! Resources: N added, 0 changed, 0 destroyed.`

- [ ] **Step 4: Post-apply manual steps (document these in a run-book comment in your ops channel)**

**A. Set GitHub Actions repository secrets**

```bash
# Get values from Terraform outputs
terraform output -raw acr_login_server
terraform output -raw github_actions_client_id
terraform output -raw github_actions_client_secret

# In GitHub repo Settings → Secrets → Actions, create:
# AZURE_CREDENTIALS  — JSON: {"clientId":"...","clientSecret":"...","subscriptionId":"...","tenantId":"..."}
# REGISTRY_LOGIN_SERVER — from acr_login_server output
# REGISTRY_USERNAME — from github_actions_client_id output
# REGISTRY_PASSWORD — from github_actions_client_secret output
```

**B. Manually create Key Vault secrets**

Secrets are created manually — they must not appear in Terraform state.

```bash
KV_URL=$(terraform output -raw key_vault_uri)

az keyvault secret set --vault-name kv-uks-vigil-01 --name "jira-api-token" --value "<value>"
az keyvault secret set --vault-name kv-uks-vigil-01 --name "jira-base-url" --value "<value>"
az keyvault secret set --vault-name kv-uks-vigil-01 --name "cisco-support-api-key" --value "<value>"
az keyvault secret set --vault-name kv-uks-vigil-01 --name "shodan-api-key" --value "<value>"
az keyvault secret set --vault-name kv-uks-vigil-01 --name "ise-tacacs-key" --value "<value>"
# Repeat for each tenant's vendor credentials:
# tenant-{id}-palo-alto-api-key, tenant-{id}-cisco-asa-token,
# tenant-{id}-cisco-meraki-api-key, tenant-{id}-fortinet-token
```

**C. Verify Container Apps environment is healthy**

```bash
az containerapp env show --name cae-uks-vigil-01 --resource-group rg-uks-vigil-01 \
  --query "properties.provisioningState"
# Expected: "Succeeded"
```

**D. Verify all 11 Container Apps are running with the placeholder image**

```bash
az containerapp list --resource-group rg-uks-vigil-01 --query "[].{name:name, state:properties.runningStatus}" -o table
# Expected: 11 rows, all state = "Running"
```

**E. Verify private endpoints are approved**

```bash
az network private-endpoint list --resource-group rg-uks-vigil-01 \
  --query "[].{name:name, state:privateLinkServiceConnections[0].privateLinkServiceConnectionState.status}" -o table
# Expected: all state = "Approved"
```

- [ ] **Step 5: Commit final state**

```bash
git add .
git commit -m "feat(terraform): complete infrastructure plan — all modules validated and post-apply steps documented"
```

---

## Known Terraform Provider Gaps

| Gap | Workaround |
|---|---|
| `azurerm` does not expose Container Apps ingress timeout | Set via `az containerapp ingress update --timeout 960 --name ca-uks-coordinator-01 --resource-group rg-uks-vigil-01` in `deploy-coordinator.yml` post-deploy step |
| `azurerm` does not expose `NET_ADMIN`/`NET_RAW` Linux capabilities on Container Apps | Set via `az containerapp update` ARM properties override in `deploy-agent-probe.yml` post-deploy step |
| `azurerm` does not manage Key Vault secrets (intentional) | Secrets created manually post-apply (Step 4B above) |
| GitHub Actions secrets cannot be set via Terraform | Set manually after SP creation (Step 4A above) |
