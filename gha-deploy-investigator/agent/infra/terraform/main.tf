locals {
  common_labels = {
    app       = "gha-deploy-investigator"
    component = "agent"
    env       = var.environment
    prefix    = var.name_prefix
  }

  agent_sa_id         = substr("${var.name_prefix}-agent-sa", 0, 30)
  tasks_invoker_sa_id = substr("${var.name_prefix}-tasks-invoker-sa", 0, 30)
  service_name        = "${var.name_prefix}-${var.service_name}"
}

# Service Account for Agent
resource "google_service_account" "agent_sa" {
  project      = var.project_id
  account_id   = local.agent_sa_id
  display_name = "GHA Deploy Investigator Agent Service Account"
}

# Service Account for Cloud Tasks Invoker
resource "google_service_account" "tasks_invoker_sa" {
  project      = var.project_id
  account_id   = local.tasks_invoker_sa_id
  display_name = "GHA Deploy Investigator Cloud Tasks Invoker SA"
}

# IAM Role Bindings for Agent SA
resource "google_project_iam_member" "agent_vertex_ai" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.agent_sa.email}"
}

resource "google_project_iam_member" "agent_logging_viewer" {
  project = var.project_id
  role    = "roles/logging.viewer"
  member  = "serviceAccount:${google_service_account.agent_sa.email}"
}

resource "google_project_iam_member" "agent_cloud_run_viewer" {
  project = var.project_id
  role    = "roles/run.viewer"
  member  = "serviceAccount:${google_service_account.agent_sa.email}"
}

# Secret Definition for GitHub App Private Key
resource "google_secret_manager_secret" "github_app_private_key" {
  project   = var.project_id
  secret_id = "${var.name_prefix}-github-app-private-key"
  labels    = local.common_labels

  replication {
    auto {}
  }
}

# Secret Accessor IAM for Agent SA
resource "google_secret_manager_secret_iam_member" "agent_gh_key_access" {
  project   = var.project_id
  secret_id = google_secret_manager_secret.github_app_private_key.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.agent_sa.email}"
}

# Cloud Run Service for Agent (Internal Ingress Only)
resource "google_cloud_run_v2_service" "agent" {
  name     = local.service_name
  location = var.region
  project  = var.project_id
  ingress  = "INGRESS_TRAFFIC_INTERNAL_ONLY"
  labels   = local.common_labels

  template {
    service_account = google_service_account.agent_sa.email
    timeout         = "${var.cloud_run_timeout_seconds}s"

    containers {
      image = "${var.region}-docker.pkg.dev/${var.project_id}/${var.repository_id}/${local.service_name}:${var.image_tag}"

      resources {
        limits = {
          cpu    = var.cloud_run_cpu
          memory = var.cloud_run_memory
        }
      }

      env {
        name  = "ENVIRONMENT"
        value = var.environment
      }
      env {
        name  = "NAME_PREFIX"
        value = var.name_prefix
      }
      env {
        name  = "GCP_PROJECT"
        value = var.project_id
      }
      env {
        name  = "GCP_REGION"
        value = var.region
      }
      env {
        name  = "BRIDGE_SERVICE_URL"
        value = var.bridge_service_url
      }
      env {
        name  = "GITHUB_APP_ID"
        value = var.github_app_id
      }
      env {
        name  = "GEMINI_MODEL"
        value = var.gemini_model
      }
      env {
        name  = "VERTEX_AI_LOCATION"
        value = var.vertex_ai_location
      }

      env {
        name = "GITHUB_APP_PRIVATE_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.github_app_private_key.secret_id
            version = "latest"
          }
        }
      }
    }
  }

  depends_on = [
    google_secret_manager_secret_iam_member.agent_gh_key_access,
    google_project_iam_member.agent_vertex_ai,
    google_project_iam_member.agent_logging_viewer,
    google_project_iam_member.agent_cloud_run_viewer,
  ]

  lifecycle {
    ignore_changes = [
      client,
      client_version,
    ]
  }
}

# Allow Cloud Tasks Invoker SA to invoke internal Agent Cloud Run service
resource "google_cloud_run_v2_service_iam_member" "tasks_invoker" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.agent.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.tasks_invoker_sa.email}"
}
