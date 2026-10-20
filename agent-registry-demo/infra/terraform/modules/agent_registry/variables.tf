variable "project_id" {
  description = "The GCP project ID"
  type        = string
}

variable "region" {
  description = "The GCP region for Agent Registry"
  type        = string
}

variable "service_id" {
  description = "Service ID for the registered tool in Agent Registry"
  type        = string
  default     = "calculator"
}

variable "display_name" {
  description = "Display name for the registered service"
  type        = string
  default     = "Precision Calculator Service"
}

variable "description" {
  description = "Description of the registered service"
  type        = string
  default     = "Provides verified arithmetic operations (add, multiply, power)"
}

variable "calculator_service_url" {
  description = "Base URL of the Calculator Cloud Run service"
  type        = string
}
