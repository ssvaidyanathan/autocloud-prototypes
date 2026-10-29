output "bridge_service_name" {
  description = "Name of the Incident Bridge Cloud Run service"
  value       = module.cloud_run.service_name
}

output "bridge_service_url" {
  description = "URL of the Incident Bridge Cloud Run service"
  value       = module.cloud_run.service_uri
}

output "bridge_service_account_email" {
  description = "Email of the Incident Bridge service account"
  value       = module.iam.bridge_service_account_email
}

output "artifact_registry_repo" {
  description = "Name/ID of the Artifact Registry Docker repository"
  value       = module.artifact_registry.repository_id
}

output "secret_ids" {
  description = "List of Secret Manager secret IDs created for credentials"
  value       = module.secrets.secret_ids
}
