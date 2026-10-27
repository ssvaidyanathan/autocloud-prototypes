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
  description = "Retry window for a failed triage task, measured from its first attempt (Duration string in seconds with an \"s\" suffix). A task still failing when the window ends is deleted. Must be greater than 0s (0s would remove the time limit) and at most \"2678400s\" (31 days, the Cloud Tasks maximum task retention: https://cloud.google.com/tasks/docs/quotas)."

  validation {
    condition     = can(regex("^[0-9]+(\\.[0-9]{1,9})?s$", var.triage_queue_max_retry_duration)) && try(tonumber(trimsuffix(var.triage_queue_max_retry_duration, "s")) > 0 && tonumber(trimsuffix(var.triage_queue_max_retry_duration, "s")) <= 2678400, false)
    error_message = "triage_queue_max_retry_duration must be a positive duration string in seconds ending in \"s\", e.g. \"3600s\", and at most \"2678400s\" (31 days, the Cloud Tasks task retention limit)."
  }
}

variable "triage_queue_min_backoff" {
  type        = string
  default     = "1s"
  description = "Initial wait before retrying a failed triage task (Duration string in seconds with an \"s\" suffix). The wait doubles on each retry (16 doublings by default) up to triage_queue_max_backoff. Must be between \"1s\" and \"2592000s\": Cloud Tasks documents backoffs as truncated to whole seconds (https://cloud.google.com/tasks/docs/reference/rpc/google.cloud.tasks.v2#retryconfig), so \"0s\" and sub-second values are rejected, and no retry can be scheduled more than 30 days ahead (https://cloud.google.com/tasks/docs/quotas)."

  validation {
    condition     = can(regex("^[0-9]+(\\.[0-9]{1,9})?s$", var.triage_queue_min_backoff)) && try(tonumber(trimsuffix(var.triage_queue_min_backoff, "s")) >= 1 && tonumber(trimsuffix(var.triage_queue_min_backoff, "s")) <= 2592000, false)
    error_message = "triage_queue_min_backoff must be a duration string in seconds ending in \"s\", between \"1s\" and \"2592000s\" (30 days), e.g. \"1s\" or \"1.5s\"."
  }
}

variable "triage_queue_max_backoff" {
  type        = string
  default     = "300s"
  description = "Maximum wait between retries of a failed triage task (Duration string in seconds with an \"s\" suffix, \"300s\" = 5 min). Must be >= triage_queue_min_backoff and at most \"2592000s\" (30 days, the Cloud Tasks maximum schedule time: https://cloud.google.com/tasks/docs/quotas)."

  validation {
    condition     = can(regex("^[0-9]+(\\.[0-9]{1,9})?s$", var.triage_queue_max_backoff)) && try(tonumber(trimsuffix(var.triage_queue_max_backoff, "s")) >= 1 && tonumber(trimsuffix(var.triage_queue_max_backoff, "s")) <= 2592000, false)
    error_message = "triage_queue_max_backoff must be a duration string in seconds ending in \"s\", between \"1s\" and \"2592000s\" (30 days), e.g. \"300s\"."
  }

  # Checked only when both values are well formed, so a malformed value reports only its format error.
  validation {
    condition     = !(can(regex("^[0-9]+(\\.[0-9]{1,9})?s$", var.triage_queue_max_backoff)) && can(regex("^[0-9]+(\\.[0-9]{1,9})?s$", var.triage_queue_min_backoff))) || try(tonumber(trimsuffix(var.triage_queue_max_backoff, "s")) >= tonumber(trimsuffix(var.triage_queue_min_backoff, "s")), true)
    error_message = "triage_queue_max_backoff must be >= triage_queue_min_backoff."
  }
}

variable "timer_queue_max_attempts" {
  type        = number
  default     = 5
  description = "Minimum attempts per wake task on the timer queue. Cloud Tasks stops retrying only when both this and timer_queue_max_retry_duration are satisfied, so a small positive value makes the retry duration the binding limit. -1 is rejected: the Cloud Tasks docs describe -1 only together with a max_retry_duration of 0 (retry until the task retention limit), so it adds nothing here. See https://cloud.google.com/tasks/docs/reference/rest/v2/RetryConfig."

  validation {
    condition     = var.timer_queue_max_attempts >= 1
    error_message = "timer_queue_max_attempts must be >= 1. -1 (unlimited) is not allowed; the retry window is set by timer_queue_max_retry_duration."
  }
}

variable "timer_queue_max_retry_duration" {
  type        = string
  default     = "86400s"
  description = "Retry window for a failed settle, max-wait, or cool-down wake, measured from its first attempt (Duration string in seconds with an \"s\" suffix). A wake still failing when the window ends is deleted. Must be greater than 0s (0s would remove the time limit) and at most \"2678400s\" (31 days, the Cloud Tasks maximum task retention: https://cloud.google.com/tasks/docs/quotas)."

  validation {
    condition     = can(regex("^[0-9]+(\\.[0-9]{1,9})?s$", var.timer_queue_max_retry_duration)) && try(tonumber(trimsuffix(var.timer_queue_max_retry_duration, "s")) > 0 && tonumber(trimsuffix(var.timer_queue_max_retry_duration, "s")) <= 2678400, false)
    error_message = "timer_queue_max_retry_duration must be a positive duration string in seconds ending in \"s\", e.g. \"3600s\", and at most \"2678400s\" (31 days, the Cloud Tasks task retention limit)."
  }
}

variable "timer_queue_min_backoff" {
  type        = string
  default     = "1s"
  description = "Initial wait before retrying a failed wake (Duration string in seconds with an \"s\" suffix). The wait doubles on each retry (16 doublings by default) up to timer_queue_max_backoff. Must be between \"1s\" and \"2592000s\": Cloud Tasks documents backoffs as truncated to whole seconds (https://cloud.google.com/tasks/docs/reference/rpc/google.cloud.tasks.v2#retryconfig), so \"0s\" and sub-second values are rejected, and no retry can be scheduled more than 30 days ahead (https://cloud.google.com/tasks/docs/quotas)."

  validation {
    condition     = can(regex("^[0-9]+(\\.[0-9]{1,9})?s$", var.timer_queue_min_backoff)) && try(tonumber(trimsuffix(var.timer_queue_min_backoff, "s")) >= 1 && tonumber(trimsuffix(var.timer_queue_min_backoff, "s")) <= 2592000, false)
    error_message = "timer_queue_min_backoff must be a duration string in seconds ending in \"s\", between \"1s\" and \"2592000s\" (30 days), e.g. \"1s\" or \"1.5s\"."
  }
}

variable "timer_queue_max_backoff" {
  type        = string
  default     = "60s"
  description = "Maximum wait between retries of a failed wake (Duration string in seconds with an \"s\" suffix, \"60s\" = 1 min), so a wake runs at most about this long after the agent recovers. Must be >= timer_queue_min_backoff and at most \"2592000s\" (30 days, the Cloud Tasks maximum schedule time: https://cloud.google.com/tasks/docs/quotas)."

  validation {
    condition     = can(regex("^[0-9]+(\\.[0-9]{1,9})?s$", var.timer_queue_max_backoff)) && try(tonumber(trimsuffix(var.timer_queue_max_backoff, "s")) >= 1 && tonumber(trimsuffix(var.timer_queue_max_backoff, "s")) <= 2592000, false)
    error_message = "timer_queue_max_backoff must be a duration string in seconds ending in \"s\", between \"1s\" and \"2592000s\" (30 days), e.g. \"60s\"."
  }

  # Checked only when both values are well formed, so a malformed value reports only its format error.
  validation {
    condition     = !(can(regex("^[0-9]+(\\.[0-9]{1,9})?s$", var.timer_queue_max_backoff)) && can(regex("^[0-9]+(\\.[0-9]{1,9})?s$", var.timer_queue_min_backoff))) || try(tonumber(trimsuffix(var.timer_queue_max_backoff, "s")) >= tonumber(trimsuffix(var.timer_queue_min_backoff, "s")), true)
    error_message = "timer_queue_max_backoff must be >= timer_queue_min_backoff."
  }
}
