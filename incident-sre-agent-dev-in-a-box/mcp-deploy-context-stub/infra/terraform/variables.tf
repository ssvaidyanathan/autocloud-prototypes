variable "project_id" {
  type        = string
  description = "The Google Cloud project ID"
  default     = "example-project-id"
}

variable "region" {
  type        = string
  description = "Google Cloud region for deployment"
  default     = "us-central1"
}

variable "service_name" {
  type        = string
  description = "Name of the Cloud Run service"
  default     = "mcp-deploy-context-stub"
}

variable "image" {
  type        = string
  description = "Container image URI for deployment context MCP stub"
  default     = "gcr.io/example-project-id/mcp-deploy-context-stub:latest"
}

variable "ingress" {
  type        = string
  description = "Cloud Run ingress traffic setting"
  default     = "internal-and-cloud-load-balancing"
}

variable "authorized_caller_service_accounts" {
  type        = list(string)
  description = "List of caller service account emails authorized to invoke MCP server"
  default = [
    "hitl-chatbot-sa@example-project-id.iam.gserviceaccount.com",
    "dib-bridge-sa@example-project-id.iam.gserviceaccount.com",
  ]
}

variable "fixtures_gcs_uri" {
  type        = string
  description = "Optional GCS URI (gs://bucket/prefix) to dynamically load incident fixtures on startup"
  default     = ""
}

variable "auth_disabled" {
  type        = bool
  description = "Whether to disable OIDC authentication (for testing only)"
  default     = false
}
