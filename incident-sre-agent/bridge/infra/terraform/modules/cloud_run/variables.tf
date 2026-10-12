variable "project_id" {
  description = "The GCP project ID"
  type        = string
}

variable "region" {
  description = "The GCP region"
  type        = string
  default     = "us-central1"
}

variable "service_name" {
  description = "Name of the Cloud Run service"
  type        = string
  default     = "incident-bridge"
}

variable "container_image" {
  description = "Container image URI for Cloud Run"
  type        = string
}

variable "service_account_email" {
  description = "Service account email under which Cloud Run executes"
  type        = string
}

variable "tasks_queue_name" {
  description = "Name of the Cloud Tasks queue"
  type        = string
  default     = "investigations"
}

variable "tasks_invoker_service_account_email" {
  description = "Email of the Cloud Tasks invoker service account"
  type        = string
}

variable "agent_service_url" {
  description = "URL of the SRE Agent Cloud Run service"
  type        = string
}

variable "agent_service_account_email" {
  description = "Email of the SRE Agent service account"
  type        = string
}

variable "callback_url" {
  description = "Callback URL for SRE Agent to deliver reports"
  type        = string
  default     = ""
}

variable "timeout_seconds" {
  description = "Cloud Run timeout in seconds"
  type        = number
  default     = 300
}

variable "cpu" {
  description = "Cloud Run CPU allocation"
  type        = string
  default     = "1"
}

variable "memory" {
  description = "Cloud Run memory allocation"
  type        = string
  default     = "512Mi"
}

variable "labels" {
  description = "Labels to apply to the Cloud Run service"
  type        = map(string)
  default     = {}
}

variable "name_prefix" {
  description = "Prefix applied to secret names"
  type        = string
  default     = "isa"
}

variable "servicenow_instance_url" {
  description = "ServiceNow instance base URL"
  type        = string
  default     = ""
}

variable "jira_base_url" {
  description = "Jira Cloud base URL"
  type        = string
  default     = ""
}

variable "environment" {
  description = "The environment name (e.g. production, staging, dev)"
  type        = string
  default     = "production"
}

