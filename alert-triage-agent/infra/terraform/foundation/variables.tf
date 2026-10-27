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

variable "triage_queue_max_attempts" {
  type        = number
  default     = 5
  description = "Minimum attempts per task on the triage queue. Cloud Tasks stops retrying only when both this and triage_queue_max_retry_duration are satisfied, so keep it low (5 is reached about 15s after the first attempt) to let the duration bound retries. Do not use -1: with -1 the attempt limit is never met and tasks retry until the 31-day task retention limit."
}

variable "triage_queue_max_retry_duration" {
  type        = string
  default     = "7200s"
  description = "How long a failed triage task keeps being retried, measured from its first attempt (Duration string, e.g. \"7200s\" = 2h). Sized so alerts survive an agent or downstream outage of up to about 2h instead of being dropped."
}

variable "triage_queue_min_backoff" {
  type        = string
  default     = "1s"
  description = "Initial wait before retrying a failed triage task (Duration string). The wait doubles on each retry up to triage_queue_max_backoff."
}

variable "triage_queue_max_backoff" {
  type        = string
  default     = "300s"
  description = "Maximum wait between retries of a failed triage task (Duration string, \"300s\" = 5 min)."
}
