variable "project_id" {
  description = "The GCP project ID"
  type        = string
}

variable "region" {
  description = "The GCP region for the Cloud Tasks queue"
  type        = string
  default     = "us-central1"
}

variable "queue_name" {
  description = "The name of the Cloud Tasks queue"
  type        = string
  default     = "investigations"
}

variable "max_attempts" {
  description = "Maximum delivery attempts"
  type        = number
  default     = 2
}
