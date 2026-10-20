variable "project_id" {
  description = "The GCP project ID"
  type        = string
}

variable "region" {
  description = "The GCP region to deploy resources into"
  type        = string
}

variable "name_prefix" {
  description = "Resource name prefix"
  type        = string
}

variable "labels" {
  description = "Resource labels"
  type        = map(string)
  default     = {}
}

variable "agent_service_name" {
  description = "Cloud Run service name for Agent Service"
  type        = string
}

variable "calculator_service_name" {
  description = "Cloud Run service name for Calculator Tool Service"
  type        = string
}

variable "agent_image" {
  description = "Container image for Agent Service"
  type        = string
}

variable "tool_image" {
  description = "Container image for Calculator Tool Service"
  type        = string
}

variable "agent_sa_email" {
  description = "Email of the Agent Service Account"
  type        = string
}

variable "calculator_tool_sa_email" {
  description = "Email of the Calculator Tool Service Account"
  type        = string
}

variable "agent_cpu" {
  description = "CPU limit for Agent Service"
  type        = string
  default     = "1"
}

variable "agent_memory" {
  description = "Memory limit for Agent Service"
  type        = string
  default     = "1Gi"
}

variable "agent_timeout_seconds" {
  description = "Request timeout in seconds for Agent Service"
  type        = number
  default     = 300
}

variable "tool_cpu" {
  description = "CPU limit for Calculator Tool Service"
  type        = string
  default     = "1"
}

variable "tool_memory" {
  description = "Memory limit for Calculator Tool Service"
  type        = string
  default     = "512Mi"
}

variable "tool_timeout_seconds" {
  description = "Request timeout in seconds for Calculator Tool Service"
  type        = number
  default     = 300
}

variable "agent_registry_endpoint" {
  description = "Base API endpoint for the GCP Agent Registry service"
  type        = string
  default     = "https://agentregistry.googleapis.com/v1alpha"
}

variable "agent_ingress" {
  description = "Ingress setting for Agent Service"
  type        = string
  default     = "INGRESS_TRAFFIC_ALL"
}

variable "tool_ingress" {
  description = "Ingress setting for Calculator Tool Service"
  type        = string
  default     = "INGRESS_TRAFFIC_INTERNAL_ONLY"
}
