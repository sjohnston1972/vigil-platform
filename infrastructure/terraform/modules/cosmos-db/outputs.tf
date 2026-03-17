output "step_up_requests_container_id" {
  value       = azurerm_cosmosdb_sql_container.step_up_requests.id
  description = "ID of the step_up_requests container"
}

output "step_up_grants_container_id" {
  value       = azurerm_cosmosdb_sql_container.step_up_grants.id
  description = "ID of the step_up_grants container"
}
