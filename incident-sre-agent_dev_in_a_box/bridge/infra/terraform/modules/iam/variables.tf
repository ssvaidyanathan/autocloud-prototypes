variable "project_id" {
  description = "The GCP project ID"
  type        = string
}

variable "region" {
  description = "The GCP region"
  type        = string
  default     = "us-central1"
}

variable "tasks_queue_name" {
  description = "Name of the Cloud Tasks queue to grant enqueuer role on"
  type        = string
  default     = "investigations"
}

variable "tasks_invoker_service_account_email" {
  description = "Email of the Cloud Tasks invoker service account to act as"
  type        = string
}

variable "bridge_service_account_id" {
  description = "Account ID for Incident Bridge service account"
  type        = string
  default     = "bridge-sa"
}
