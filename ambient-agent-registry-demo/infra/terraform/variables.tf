variable "name_prefix" {
  description = "Prefix applied to all created resource names and labels (e.g. ambient)"
  type        = string
  default     = "ambient"

  validation {
    condition     = can(regex("^[a-z]([a-z0-9-]{0,8}[a-z0-9])?$", var.name_prefix))
    error_message = "name_prefix must start with a lowercase letter, contain only lowercase alphanumeric characters and hyphens, not end with a hyphen, and be at most 10 characters."
  }
}

variable "project_id" {
  description = "The GCP project ID"
  type        = string
}

variable "region" {
  description = "The GCP region to deploy resources into"
  type        = string
  default     = "us-central1"
}

variable "environment" {
  description = "The deployment environment (e.g. demo, staging, prod)"
  type        = string
  default     = "demo"
}

variable "agent_image" {
  description = "Container image for the agent service. If not specified, defaults to the image in the artifact registry repository."
  type        = string
  default     = null
}

variable "tool_image" {
  description = "Container image for the calculator tool service. If not specified, defaults to the image in the artifact registry repository."
  type        = string
  default     = null
}

variable "calculator_image" {
  description = "Alias for tool_image: Container image for the calculator tool service"
  type        = string
  default     = null
}

variable "agent_cpu" {
  description = "Cloud Run CPU limit for the agent service"
  type        = string
  default     = "1"
}

variable "agent_memory" {
  description = "Cloud Run memory limit for the agent service"
  type        = string
  default     = "1Gi"
}

variable "agent_timeout_seconds" {
  description = "Cloud Run request timeout in seconds for the agent service"
  type        = number
  default     = 300
}

variable "tool_cpu" {
  description = "Cloud Run CPU limit for the calculator tool service"
  type        = string
  default     = "1"
}

variable "tool_memory" {
  description = "Cloud Run memory limit for the calculator tool service"
  type        = string
  default     = "512Mi"
}

variable "tool_timeout_seconds" {
  description = "Cloud Run request timeout in seconds for the calculator tool service"
  type        = number
  default     = 300
}

variable "agent_registry_endpoint" {
  description = "Base API endpoint for the GCP Agent Registry service"
  type        = string
  default     = "https://agentregistry.googleapis.com/v1alpha"
}

variable "agent_ingress" {
  description = "Ingress traffic setting for the Agent Cloud Run service"
  type        = string
  default     = "INGRESS_TRAFFIC_ALL"
}

variable "tool_ingress" {
  description = "Ingress traffic setting for the Calculator Tool Cloud Run service (INGRESS_TRAFFIC_INTERNAL_ONLY for VPC/internal microservice isolation, or INGRESS_TRAFFIC_ALL for serverless demo invocation without VPC connector)"
  type        = string
  default     = "INGRESS_TRAFFIC_ALL"
}

variable "skill_name" {
  description = "Exact skill resource ID in GCP Agent Registry"
  type        = string
  default     = "private-ambient-privilege-audit-skill"
}

