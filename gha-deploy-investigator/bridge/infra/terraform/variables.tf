variable "name_prefix" {
  description = "Prefix applied to all created resource names and labels"
  type        = string
  default     = "gdi"

  validation {
    condition     = can(regex("^[a-z]([a-z0-9-]{0,8}[a-z0-9])?$", var.name_prefix))
    error_message = "name_prefix must start with a lowercase letter, contain only lowercase alphanumeric characters and hyphens, not end with a hyphen, and be at most 10 characters."
  }
}

variable "project_id" {
  description = "The GCP project ID"
  type        = string
}

variable "region" {
  description = "The GCP region to deploy resources into"
  type        = string
  default     = "us-central1"
}

variable "environment" {
  description = "The environment name (e.g. production, staging, dev)"
  type        = string
  default     = "production"
}

variable "image_tag" {
  description = "Container image tag for Cloud Run"
  type        = string
  default     = "latest"
}

variable "repository_id" {
  description = "Artifact Registry repository ID for GDI Bridge"
  type        = string
  default     = "gdi-bridge"
}

variable "service_name" {
  description = "Cloud Run service name for GDI Bridge"
  type        = string
  default     = "bridge"
}

variable "tasks_queue_name" {
  description = "Name of the Cloud Tasks queue"
  type        = string
  default     = "tasks-queue"
}

variable "tasks_invoker_service_account_email" {
  description = "Email of the Cloud Tasks invoker service account"
  type        = string
  default     = ""
}

variable "agent_service_url" {
  description = "URL of the internal Agent Cloud Run service"
  type        = string
  default     = ""
}

variable "agent_service_account_email" {
  description = "Email of the Agent service account"
  type        = string
  default     = ""
}

variable "callback_url" {
  description = "Callback URL for agent to post results back to bridge"
  type        = string
  default     = ""
}

variable "servicenow_instance_url" {
  description = "ServiceNow instance base URL (e.g. https://instance.service-now.com)"
  type        = string
  default     = ""
}

variable "servicenow_username" {
  description = "ServiceNow integration username"
  type        = string
  default     = ""
}

variable "cloud_run_timeout_seconds" {
  description = "Cloud Run timeout in seconds for GDI Bridge"
  type        = number
  default     = 300
}

variable "cloud_run_cpu" {
  description = "Cloud Run CPU allocation for GDI Bridge"
  type        = string
  default     = "1"
}

variable "cloud_run_memory" {
  description = "Cloud Run memory allocation for GDI Bridge"
  type        = string
  default     = "512Mi"
}

variable "github_webhook_secret_id" {
  description = "Secret Manager secret ID for GitHub webhook HMAC secret (defaults to ${var.name_prefix}-github-webhook-secret)"
  type        = string
  default     = ""
}

variable "servicenow_password_secret_id" {
  description = "Secret Manager secret ID for ServiceNow password (defaults to ${var.name_prefix}-servicenow-password, e.g. gdi-bridge-servicenow-password)"
  type        = string
  default     = ""
}

variable "servicenow_webhook_token_secret_id" {
  description = "Secret Manager secret ID for ServiceNow webhook token (defaults to ${var.name_prefix}-servicenow-webhook-token, e.g. gdi-bridge-servicenow-webhook-token)"
  type        = string
  default     = ""
}

variable "servicenow_username_secret_id" {
  description = "Optional Secret Manager secret ID for ServiceNow username (e.g. gdi-bridge-servicenow-username). If set, read from secret; otherwise use servicenow_username variable."
  type        = string
  default     = ""
}
