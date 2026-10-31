variable "name_prefix" {
  description = "Prefix applied to all created resource names and labels (e.g. isa, acme)"
  type        = string
  default     = "isa"

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

variable "tasks_queue_name" {
  description = "Name of the investigations Cloud Tasks queue"
  type        = string
  default     = "investigations"
}

variable "tasks_invoker_service_account_email" {
  description = "Email of the Cloud Tasks invoker service account (from agent outputs)"
  type        = string
}

variable "agent_service_url" {
  description = "URL of the private SRE Agent Cloud Run service (from agent outputs)"
  type        = string
}

variable "agent_service_account_email" {
  description = "Email of the SRE Agent service account (from agent outputs)"
  type        = string
}

variable "callback_url" {
  description = "Callback URL for agent to post results (defaults to bridge Cloud Run URL if empty)"
  type        = string
  default     = ""
}

variable "repository_id" {
  description = "Artifact Registry repository ID for Incident Bridge"
  type        = string
  default     = "incident-bridge"
}

variable "service_name" {
  description = "Cloud Run service name for Incident Bridge"
  type        = string
  default     = "incident-bridge"
}

variable "bridge_service_account_id" {
  description = "Account ID for Incident Bridge service account"
  type        = string
  default     = "bridge-sa"
}

variable "cloud_run_timeout_seconds" {
  description = "Cloud Run timeout in seconds for Incident Bridge"
  type        = number
  default     = 300
}

variable "cloud_run_cpu" {
  description = "Cloud Run CPU allocation for Incident Bridge"
  type        = string
  default     = "1"
}

variable "cloud_run_memory" {
  description = "Cloud Run memory allocation for Incident Bridge"
  type        = string
  default     = "512Mi"
}

variable "servicenow_instance_url" {
  description = "ServiceNow instance base URL (e.g. https://your-instance.service-now.com)"
  type        = string
  default     = ""
}

variable "jira_base_url" {
  description = "Jira Cloud base URL (e.g. https://your-domain.atlassian.net)"
  type        = string
  default     = ""
}
