variable "project_id" {
  type        = string
  default     = "cloud-assist-fde-2"
  description = "Google Cloud project ID"
}

variable "region" {
  type        = string
  default     = "us-west1"
  description = "Google Cloud region"
}

variable "name_prefix" {
  type        = string
  default     = "ata"
  description = "Resource name prefix for alert policies"
}

variable "bridge_url" {
  type        = string
  default     = "https://ata-bridge-4klvlqnmpa-uw.a.run.app"
  description = "Base URL of ata-bridge Cloud Run service"
}

variable "cloudsql_connections_threshold" {
  type        = number
  default     = 20
  description = "Connection count threshold for Cloud SQL critical alert (~80% of db-f1-micro max_connections 25)"
}

variable "notification_channel_id" {
  type        = string
  default     = null
  description = "Notification channel resource name for ata-bridge webhook. If null, sourced from foundation remote state."
}
