variable "project_id" {
  description = "The GCP project ID"
  type        = string
}

variable "agent_service_account_id" {
  description = "Service account ID for the agent service"
  type        = string
}

variable "calculator_tool_service_account_id" {
  description = "Service account ID for the calculator tool service"
  type        = string
}
