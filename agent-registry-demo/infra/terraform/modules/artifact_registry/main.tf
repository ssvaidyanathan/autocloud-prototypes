resource "google_artifact_registry_repository" "repo" {
  project       = var.project_id
  location      = var.region
  repository_id = var.repository_id
  description   = "Docker repository for Agent Registry Demo images"
  format        = "DOCKER"
  labels        = var.labels
}
