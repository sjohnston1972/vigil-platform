variable "ingress_timeout_seconds" {
  type        = number
  description = "HTTP request timeout in seconds. Must exceed the longest step_up pending_ttl_seconds + 60."
  default     = 960 # 900s (15min TTL) + 60s buffer
}
