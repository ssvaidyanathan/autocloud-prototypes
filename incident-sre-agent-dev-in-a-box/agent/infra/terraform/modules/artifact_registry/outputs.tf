output "repository_id" {
  description = "Artifact Registry repository ID"
  value       = google_artifact_registry_repository.repo.repository_id
}

output "repository_name" {
  description = "Artifact Registry repository full resource name"
  value       = google_artifact_registry_repository.repo.name
}
