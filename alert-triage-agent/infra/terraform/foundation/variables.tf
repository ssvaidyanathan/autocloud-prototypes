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
  description = "Minimum attempts per task on the triage queue. Cloud Tasks stops retrying only when both this and triage_queue_max_retry_duration are satisfied, so a small positive value (5 is reached about 15s after the first attempt) makes the retry duration the binding limit. -1 is rejected: the Cloud Tasks docs describe -1 only together with a max_retry_duration of 0 (retry until the task retention limit), so it adds nothing here. See https://cloud.google.com/tasks/docs/reference/rest/v2/RetryConfig."

  validation {
    condition     = var.triage_queue_max_attempts >= 1
    error_message = "triage_queue_max_attempts must be >= 1. -1 (unlimited) is not allowed; the retry window is set by triage_queue_max_retry_duration."
  }
}

variable "triage_queue_max_retry_duration" {
  type        = string
  default     = "86400s"
  description = "Retry window for a failed triage task, measured from its first attempt (Duration string in seconds with an \"s\" suffix). A task still failing when the window ends is deleted. Must be greater than 0s: 0s would remove the time limit."

  validation {
    condition     = can(regex("^[0-9]+(\\.[0-9]{1,9})?s$", var.triage_queue_max_retry_duration)) && try(tonumber(trimsuffix(var.triage_queue_max_retry_duration, "s")) > 0, false)
    error_message = "triage_queue_max_retry_duration must be a positive duration string in seconds ending in \"s\", e.g. \"3600s\"."
  }
}

variable "triage_queue_min_backoff" {
  type        = string
  default     = "1s"
  description = "Initial wait before retrying a failed triage task (Duration string in seconds with an \"s\" suffix). The wait doubles on each retry (16 doublings by default) up to triage_queue_max_backoff."

  validation {
    condition     = can(regex("^[0-9]+(\\.[0-9]{1,9})?s$", var.triage_queue_min_backoff))
    error_message = "triage_queue_min_backoff must be a duration string in seconds ending in \"s\", e.g. \"1s\" or \"0.5s\"."
  }
}

variable "triage_queue_max_backoff" {
  type        = string
  default     = "300s"
  description = "Maximum wait between retries of a failed triage task (Duration string in seconds with an \"s\" suffix, \"300s\" = 5 min). Must be >= triage_queue_min_backoff."

  validation {
    condition     = can(regex("^[0-9]+(\\.[0-9]{1,9})?s$", var.triage_queue_max_backoff))
    error_message = "triage_queue_max_backoff must be a duration string in seconds ending in \"s\", e.g. \"300s\"."
  }

  validation {
    condition     = try(tonumber(trimsuffix(var.triage_queue_max_backoff, "s")) >= tonumber(trimsuffix(var.triage_queue_min_backoff, "s")), false)
    error_message = "triage_queue_max_backoff must be >= triage_queue_min_backoff."
  }
}
