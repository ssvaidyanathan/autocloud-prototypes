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
