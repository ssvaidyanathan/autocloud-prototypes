variable "name_prefix" {
  description = "Prefix applied to all created resource names and labels (e.g. isa, acme)"
  type        = string
  default     = "isa"

  validation {
    condition     = can(regex("^[a-z]([a-z0-9-]{0,8}[a-z0-9])?$", var.name_prefix))
    error_message = "name_prefix must start with a lowercase letter, contain only lowercase alphanumeric characters and hyphens, not end with a hyphen, and be at most 10 characters."
  }
}

variable "environment" {
  description = "The environment name (e.g. production, staging, dev, demo)"
  type        = string
  default     = "demo"
}

variable "project_id" {
  type        = string
  description = "GCP Project ID"
}

variable "region" {
  type        = string
  description = "GCP region for resources"
  default     = "us-central1"
}

variable "image_tag" {
  type        = string
  description = "Container image tag for demo services"
  default     = "latest"
}

variable "shop_api_image" {
  type        = string
  description = "Container image for shop-api (defaults to regional Artifact Registry image if empty)"
  default     = ""
}

variable "inventory_svc_image" {
  type        = string
  description = "Container image for inventory-svc (defaults to regional Artifact Registry image if empty)"
  default     = ""
}

variable "shop_api_service_name" {
  type        = string
  description = "Cloud Run service name for shop-api"
  default     = "shop-api"
}

variable "inventory_svc_service_name" {
  type        = string
  description = "Cloud Run service name for inventory-svc"
  default     = "inventory-svc"
}

variable "shop_api_sa_account_id" {
  type        = string
  description = "Service account ID for shop-api"
  default     = "shop-api-sa"
}

variable "cloud_run_timeout_seconds" {
  type        = number
  description = "Timeout in seconds for demo Cloud Run services"
  default     = 300
}

variable "cloud_run_cpu" {
  type        = string
  description = "CPU allocation for demo Cloud Run services"
  default     = "1"
}

variable "cloud_run_memory" {
  type        = string
  description = "Memory allocation for demo Cloud Run services"
  default     = "512Mi"
}

variable "payment_gateway_key" {
  type        = string
  description = "Payment gateway API key for shop-api"
  default     = "demo-live-payment-key-xyz"
  sensitive   = true
}

variable "shop_api_cpu" {
  type        = string
  description = "CPU limit for shop-api Cloud Run service"
  default     = "1"
}

variable "shop_api_memory" {
  type        = string
  description = "Memory limit for shop-api Cloud Run service"
  default     = "1Gi"
}
