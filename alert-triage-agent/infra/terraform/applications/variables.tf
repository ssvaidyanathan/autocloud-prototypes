variable "project_id" {
  type        = string
  description = "Google Cloud project ID. No default: it identifies the target."
}

variable "region" {
  type        = string
  description = "Google Cloud region (e.g. us-central1). No default: it identifies the target." # prefix-ok: example region
}

variable "name_prefix" {
  type        = string
  description = "Resource name prefix for all alert triage agent components: lowercase, starts with a letter, 2-10 characters of [a-z0-9]. No default: it identifies the stack."

  validation {
    condition     = can(regex("^[a-z][a-z0-9]{1,9}$", var.name_prefix))
    error_message = "name_prefix must be 2-10 characters, lowercase letters and digits only, starting with a letter (regex ^[a-z][a-z0-9]{1,9}$)."
  }
}

variable "inventory_image" {
  type        = string
  description = "Container image of the inventory API, built into the stack's repository (<region>-docker.pkg.dev/<project_id>/<name_prefix>-docker/<name_prefix>-inventory-api@sha256:...). Required: no image exists in a fresh project until the build script pushes it."
}

variable "orders_image" {
  type        = string
  description = "Container image of the orders API, built into the stack's repository (<region>-docker.pkg.dev/<project_id>/<name_prefix>-docker/<name_prefix>-orders-api@sha256:...). Required: no image exists in a fresh project until the build script pushes it."
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
  description = "Serve <name_prefix>-inventory-api from the in-memory catalog instead of Cloud SQL. Rendered into ATA_INVENTORY_MOCK_DB."
}

variable "demo_max_instance_count" {
  type        = number
  default     = 1
  description = "Max Cloud Run instances per revision of the demo workloads (<name_prefix>-inventory-api, <name_prefix>-orders-api). Default 1 for this demo stack: fault state (/admin/fault/inject) and leaked DB connections (/admin/leak-connections) live in one instance's memory, and /admin/fault/reset clears only the instance it reaches. With 2 instances (live 2026-10-27 13:37-13:39Z, during the S4 reset) the reset reached the instance without the leak, and a fault would apply to only part of the traffic. One instance handles the demo load (S1-S5 send a few requests per second). This is a revision template setting: applying it creates a new revision, and a revision pinned by name keeps the limit it was created with. The service-level limit (gcloud run services update --max, which covers every revision with no new revision) is not managed here: the google provider 6.x service-level scaling block has no max instance count, and the services ignore_changes their service-level scaling. scripts/set-demo-instance-caps.sh sets it; re-run its dry run after any apply here. Keep ATA_DEMO_MAX_INSTANCES (scripts/scenarios/common.sh, default 1) equal to this value."

  validation {
    condition     = var.demo_max_instance_count >= 1 && floor(var.demo_max_instance_count) == var.demo_max_instance_count
    error_message = "demo_max_instance_count must be a whole number >= 1."
  }
}
