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

variable "vertex_location" {
  description = "Vertex AI endpoint location for Gemini models (Gemini 3.x requires global)"
  type        = string
  default     = "global"
}

variable "repository_id" {
  description = "Artifact Registry repository ID"
  type        = string
  default     = "sre-agent"
}

variable "queue_name" {
  description = "Cloud Tasks queue name for investigations"
  type        = string
  default     = "investigations"
}

variable "service_name" {
  description = "Cloud Run service name for SRE Agent"
  type        = string
  default     = "sre-agent"
}

variable "agent_service_account_id" {
  description = "Service account account_id for SRE Agent"
  type        = string
  default     = "agent-sa"
}

variable "tasks_invoker_service_account_id" {
  description = "Service account account_id for Cloud Tasks invoker"
  type        = string
  default     = "tasks-invoker-sa"
}

variable "cloud_run_timeout_seconds" {
  description = "Cloud Run request timeout in seconds"
  type        = number
  default     = 900
}

variable "cloud_run_cpu" {
  description = "Cloud Run CPU limit"
  type        = string
  default     = "2"
}

variable "cloud_run_memory" {
  description = "Cloud Run memory limit"
  type        = string
  default     = "2Gi"
}

variable "queue_max_attempts" {
  description = "Maximum delivery attempts for Cloud Tasks queue"
  type        = number
  default     = 2
}

variable "model_dispatcher" {
  description = "Gemini model for top-level investigation coordination"
  type        = string
  default     = "gemini-3.8-flash"
}

variable "model_synthesizer" {
  description = "Gemini model for root cause synthesis"
  type        = string
  default     = "gemini-3.8-flash"
}

variable "model_specialist" {
  description = "Gemini model for domain specialist subagents"
  type        = string
  default     = "gemini-3.8-flash"
}

variable "allowed_callback_hosts" {
  description = "Comma-delimited wildcard hosts allowed for HTTP callbacks"
  type        = string
  default     = "*.run.app"
}

