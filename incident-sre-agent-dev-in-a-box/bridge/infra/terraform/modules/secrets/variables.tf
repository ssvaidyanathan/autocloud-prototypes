variable "project_id" {
  description = "The GCP project ID"
  type        = string
}

variable "bridge_service_account_email" {
  description = "Email of the Incident Bridge service account"
  type        = string
}

variable "name_prefix" {
  description = "Prefix applied to Secret Manager secret IDs"
  type        = string
  default     = "isa"
}

variable "labels" {
  description = "Labels applied to Secret Manager secrets"
  type        = map(string)
  default     = {}
}
