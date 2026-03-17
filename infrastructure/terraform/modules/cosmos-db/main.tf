resource "azurerm_cosmosdb_sql_container" "step_up_requests" {
  name                  = "step_up_requests"
  resource_group_name   = var.resource_group_name
  account_name          = var.cosmos_account_name
  database_name         = var.cosmos_database_name
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
  account_name          = var.cosmos_account_name
  database_name         = var.cosmos_database_name
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
