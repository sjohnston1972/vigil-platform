variable "resource_group_name" {
  type        = string
  description = "Azure resource group name."
}

variable "cosmos_account_name" {
  type        = string
  description = "Cosmos DB account name."
}

variable "cosmos_database_name" {
  type        = string
  description = "Cosmos DB database name."
}

variable "tags" {
  type        = map(string)
  description = "Tags to apply to all resources."
  default     = {}
}
