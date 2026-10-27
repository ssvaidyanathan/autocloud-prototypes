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
  description = "Resource name prefix for all alert triage agent components"
}

variable "inventory_image" {
  type        = string
  default     = "us-west1-docker.pkg.dev/cloud-assist-fde-2/ata-docker/ata-inventory-api:latest"
  description = "Container image for ata-inventory-api"
}

variable "orders_image" {
  type        = string
  default     = "us-west1-docker.pkg.dev/cloud-assist-fde-2/ata-docker/ata-orders-api:latest"
  description = "Container image for ata-orders-api"
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

variable "inventory_mock_db" {
  type        = bool
  default     = false
  description = "Serve ata-inventory-api from the in-memory catalog instead of Cloud SQL. Rendered into both ATA_INVENTORY_MOCK_DB and its legacy alias MOCK_DB."
}

variable "demo_max_instance_count" {
  type        = number
  default     = 1
  description = "Max Cloud Run instances per revision of the demo workloads (ata-inventory-api, ata-orders-api). Default 1 for this demo stack: fault state (/admin/fault/inject) and leaked DB connections (/admin/leak-connections) live in one instance's memory, and /admin/fault/reset clears only the instance it reaches. With 2 instances (live 2026-10-27 13:37-13:39Z, during the S4 reset) the reset reached the instance without the leak, and a fault would apply to only part of the traffic. One instance handles the demo load (S1-S5 send a few requests per second). This is a revision template setting: applying it creates a new revision, and a revision pinned by name keeps the limit it was created with."

  validation {
    condition     = var.demo_max_instance_count >= 1 && floor(var.demo_max_instance_count) == var.demo_max_instance_count
    error_message = "demo_max_instance_count must be a whole number >= 1."
  }
}
