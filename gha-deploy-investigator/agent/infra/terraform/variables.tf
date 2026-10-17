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
  description = "Artifact Registry repository ID for GDI Agent"
  type        = string
  default     = "gdi-agent"
}

variable "service_name" {
  description = "Cloud Run service name for GDI Agent"
  type        = string
  default     = "agent"
}

variable "bridge_service_url" {
  description = "URL of the Bridge Cloud Run service for callbacks"
  type        = string
  default     = ""
}

variable "github_app_id" {
  description = "GitHub App ID for Check Run writeback"
  type        = string
  default     = ""
}

variable "cloud_run_timeout_seconds" {
  description = "Cloud Run timeout in seconds for GDI Agent"
  type        = number
  default     = 900
}

variable "cloud_run_cpu" {
  description = "Cloud Run CPU allocation for GDI Agent"
  type        = string
  default     = "2"
}

variable "cloud_run_memory" {
  description = "Cloud Run memory allocation for GDI Agent"
  type        = string
  default     = "2Gi"
}
