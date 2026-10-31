variable "project_id" {
  description = "The GCP project ID"
  type        = string
}

variable "environment" {
  description = "Deployment environment"
  type        = string
  default     = "production"
}

variable "agent_service_account_id" {
  description = "Account ID for SRE Agent service account"
  type        = string
  default     = "agent-sa"
}

variable "tasks_invoker_service_account_id" {
  description = "Account ID for Tasks invoker service account"
  type        = string
  default     = "tasks-invoker-sa"
}
