variable "resource_group_name" {
  type        = string
  description = "Name of the Azure resource group"
}

variable "cosmos_account_name" {
  type        = string
  description = "Name of the Cosmos DB account"
}

variable "cosmos_database_name" {
  type        = string
  description = "Name of the Cosmos DB database"
}

variable "tags" {
  type        = map(string)
  description = "Tags to apply to all resources"
  default     = {}
}
