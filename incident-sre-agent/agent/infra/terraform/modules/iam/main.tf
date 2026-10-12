resource "google_service_account" "agent_sa" {
  project      = var.project_id
  account_id   = var.agent_service_account_id
  display_name = "Incident SRE Agent Service Account"
  description  = "Service account for SRE Agent with strictly read-only GCP telemetry permissions and Vertex AI user"
}

resource "google_service_account" "tasks_invoker_sa" {
  project      = var.project_id
  account_id   = var.tasks_invoker_service_account_id
  display_name = "Cloud Tasks Invoker Service Account"
  description  = "Service account used by Cloud Tasks to securely invoke private Cloud Run service"
}

locals {
  agent_roles = [
    "roles/logging.viewer",
    "roles/monitoring.viewer",
    "roles/run.viewer",
    "roles/compute.viewer",
    "roles/container.viewer",
    "roles/cloudsql.viewer",
    "roles/cloudbuild.builds.viewer",
    "roles/aiplatform.user",
  ]
}

resource "google_project_iam_member" "agent_roles" {
  for_each = toset(local.agent_roles)

  project = var.project_id
  role    = each.key
  member  = "serviceAccount:${google_service_account.agent_sa.email}"
}
