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
  description = "The name of the Cloud Run service"
  type        = string
  default     = "sre-agent"
}

variable "container_image" {
  description = "The container image to run"
  type        = string
}

variable "service_account_email" {
  description = "The service account email for the Cloud Run service"
  type        = string
}

variable "invoker_service_account_email" {
  description = "The service account email permitted to invoke the service (e.g. tasks-invoker-sa)"
  type        = string
}

variable "ingress" {
  description = "Ingress settings for the service"
  type        = string
  default     = "INGRESS_TRAFFIC_INTERNAL_ONLY"
}

variable "vertex_location" {
  description = "Vertex AI endpoint location for Gemini models"
  type        = string
  default     = "global"
}

variable "timeout_seconds" {
  description = "Cloud Run timeout in seconds"
  type        = number
  default     = 900
}

variable "cpu" {
  description = "CPU allocation for Cloud Run"
  type        = string
  default     = "2"
}

variable "memory" {
  description = "Memory allocation for Cloud Run"
  type        = string
  default     = "2Gi"
}

variable "labels" {
  description = "Labels to apply to the Cloud Run service"
  type        = map(string)
  default     = {}
}

variable "environment" {
  description = "The deployment environment (e.g. production, staging, local)"
  type        = string
  default     = "production"
}

variable "name_prefix" {
  description = "Prefix applied to all created resource names and labels"
  type        = string
  default     = "isa"
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

variable "specialist_max_llm_calls" {
  description = "Maximum LLM turns allowed per specialist during investigation"
  type        = number
  default     = 8
}

variable "specialist_max_tool_calls" {
  description = "Maximum tool calls allowed per specialist during investigation"
  type        = number
  default     = 5
}

