variable "project_id" {
  type        = string
  description = "Google Cloud project ID. No default: it identifies the target."
}

variable "region" {
  type        = string
  description = "Google Cloud region (e.g. us-central1). No default: it identifies the target."
}

variable "name_prefix" {
  type        = string
  default     = "ata"
  description = "Resource name prefix for all alert triage agent components: lowercase, starts with a letter, 2-10 characters of [a-z0-9]."

  validation {
    condition     = can(regex("^[a-z][a-z0-9]{1,9}$", var.name_prefix))
    error_message = "name_prefix must be 2-10 characters, lowercase letters and digits only, starting with a letter (regex ^[a-z][a-z0-9]{1,9}$)."
  }
}
