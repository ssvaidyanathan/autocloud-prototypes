output "wif_provider_name" {
  description = "Workload Identity Provider resource name"
  value       = google_iam_workload_identity_pool_provider.provider.name
}

output "service_account_email" {
  description = "Service account email used by GitHub Actions"
  value       = google_service_account.demo_deployer_sa.email
}

output "artifact_registry_repo" {
  description = "Artifact Registry Docker repository URL"
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.demo_repo.repository_id}"
}
