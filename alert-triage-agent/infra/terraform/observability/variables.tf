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

variable "latency_per_series_aligner" {
  type        = string
  default     = "ALIGN_DELTA"
  description = "Per-series aligner of the Cloud Run latency conditions. ALIGN_DELTA keeps each series a distribution so the cross-series reducer can merge them."
}

variable "latency_cross_series_reducer" {
  type        = string
  default     = "REDUCE_PERCENTILE_99"
  description = "Cross-series reducer of the Cloud Run latency conditions. Merges the response_code and revision series into one latency percentile per group, so one incident opens per service instead of one per series."
}

variable "latency_group_by" {
  type        = list(string)
  default     = ["resource.label.service_name"]
  description = "Group-by fields of the Cloud Run latency conditions: one time series, and so at most one open incident, per value."
}

variable "alert_evaluation_missing_data" {
  type        = string
  default     = "EVALUATION_MISSING_DATA_INACTIVE"
  description = "How the latency and 5xx conditions treat missing data. INACTIVE evaluates no data as not violating, so an incident closes soon after its series stops reporting (e.g. traffic stopped) instead of waiting for auto_close. Tradeoff: an outage that stops all traffic or metrics also closes the incident; absence of traffic is not detected by these policies."

  validation {
    condition = contains([
      "EVALUATION_MISSING_DATA_INACTIVE",
      "EVALUATION_MISSING_DATA_ACTIVE",
      "EVALUATION_MISSING_DATA_NO_OP",
    ], var.alert_evaluation_missing_data)
    error_message = "alert_evaluation_missing_data must be EVALUATION_MISSING_DATA_INACTIVE, EVALUATION_MISSING_DATA_ACTIVE or EVALUATION_MISSING_DATA_NO_OP."
  }
}
