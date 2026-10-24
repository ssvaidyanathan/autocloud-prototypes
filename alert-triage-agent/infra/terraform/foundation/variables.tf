variable "project_id" {
  type        = string
  description = "The Google Cloud Project ID"
}

variable "region" {
  type        = string
  default     = "us-west1"
  description = "The default GCP region for resources"
}

variable "name_prefix" {
  type        = string
  default     = "ata"
  description = "Resource name prefix for all alert triage agent components"
}

variable "environment" {
  type        = string
  default     = "demo"
  description = "Deployment environment name"
}

variable "owner" {
  type        = string
  default     = "sre-prototypes"
  description = "Ownership team for resources"
}

variable "servicenow_instance_url" {
  type        = string
  description = "ServiceNow instance base URL"
}
