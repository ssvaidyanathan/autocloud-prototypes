locals {
  common_labels = {
    app       = "gha-deploy-investigator"
    component = "bridge"
    env       = var.environment
    prefix    = var.name_prefix
  }

  service_account_id = substr("${var.name_prefix}-bridge-sa", 0, 30)
  queue_name         = "${var.name_prefix}-${var.tasks_queue_name}"
  service_name       = "${var.name_prefix}-${var.service_name}"
}

# Service Account for Bridge
resource "google_service_account" "bridge_sa" {
  project      = var.project_id
  account_id   = local.service_account_id
  display_name = "GHA Deploy Investigator Bridge Service Account"
}

# Cloud Tasks Queue for asynchronous investigation dispatch
resource "google_cloud_tasks_queue" "tasks_queue" {
  project  = var.project_id
  location = var.region
  name     = local.queue_name

  rate_limits {
    max_concurrent_dispatches = 10
    max_dispatches_per_second = 5
  }

  retry_config {
    max_attempts = 3
    min_backoff  = "1s"
    max_backoff  = "10s"
  }
}

# Cloud Tasks enqueuer permission for Bridge SA
resource "google_cloud_tasks_queue_iam_member" "bridge_task_enqueuer" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_tasks_queue.tasks_queue.name
  role     = "roles/cloudtasks.enqueuer"
  member   = "serviceAccount:${google_service_account.bridge_sa.email}"
}

# Allow Bridge SA to impersonate/actAs the Tasks Invoker SA when generating Cloud Tasks OIDC tokens
resource "google_service_account_iam_member" "bridge_tasks_invoker_user" {
  service_account_id = "projects/${var.project_id}/serviceAccounts/${var.tasks_invoker_service_account_email}"
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.bridge_sa.email}"
}

# Secret Definitions in Secret Manager
resource "google_secret_manager_secret" "github_webhook_secret" {
  project   = var.project_id
  secret_id = "${var.name_prefix}-github-webhook-secret"
  labels    = local.common_labels

  replication {
    auto {}
  }
}

resource "google_secret_manager_secret" "servicenow_username" {
  project   = var.project_id
  secret_id = "${var.name_prefix}-servicenow-username"
  labels    = local.common_labels

  replication {
    auto {}
  }
}

resource "google_secret_manager_secret" "servicenow_password" {
  project   = var.project_id
  secret_id = "${var.name_prefix}-servicenow-password"
  labels    = local.common_labels

  replication {
    auto {}
  }
}

# Reference Agent-owned GitHub App Private Key Secret
data "google_secret_manager_secret" "github_app_private_key" {
  project   = var.project_id
  secret_id = "${var.name_prefix}-github-app-private-key"
}

resource "google_secret_manager_secret_iam_member" "bridge_gh_key_access" {
  project   = var.project_id
  secret_id = data.google_secret_manager_secret.github_app_private_key.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.bridge_sa.email}"
}

resource "google_secret_manager_secret" "servicenow_webhook_token" {
  project   = var.project_id
  secret_id = "${var.name_prefix}-servicenow-webhook-token"
  labels    = local.common_labels

  replication {
    auto {}
  }
}

# Secret Accessor IAM bindings for Bridge SA
resource "google_secret_manager_secret_iam_member" "bridge_gh_secret_access" {
  project   = var.project_id
  secret_id = google_secret_manager_secret.github_webhook_secret.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.bridge_sa.email}"
}

resource "google_secret_manager_secret_iam_member" "bridge_snow_username_access" {
  project   = var.project_id
  secret_id = google_secret_manager_secret.servicenow_username.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.bridge_sa.email}"
}

resource "google_secret_manager_secret_iam_member" "bridge_snow_pwd_access" {
  project   = var.project_id
  secret_id = google_secret_manager_secret.servicenow_password.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.bridge_sa.email}"
}

resource "google_secret_manager_secret_iam_member" "bridge_snow_token_access" {
  project   = var.project_id
  secret_id = google_secret_manager_secret.servicenow_webhook_token.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.bridge_sa.email}"
}

# Cloud Run Service for Bridge
resource "google_cloud_run_v2_service" "bridge" {
  name     = local.service_name
  location = var.region
  project  = var.project_id
  ingress  = "INGRESS_TRAFFIC_ALL"
  labels   = local.common_labels

  template {
    service_account = google_service_account.bridge_sa.email
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
        name  = "TASKS_QUEUE"
        value = google_cloud_tasks_queue.tasks_queue.name
      }
      env {
        name  = "TASKS_INVOKER_SA"
        value = var.tasks_invoker_service_account_email
      }
      env {
        name  = "AGENT_SERVICE_URL"
        value = var.agent_service_url
      }
      env {
        name  = "CALLBACK_URL"
        value = var.callback_url
      }
      env {
        name  = "SERVICENOW_INSTANCE_URL"
        value = var.servicenow_instance_url
      }
      env {
        name  = "ADAPTER_TIMEOUT_SECONDS"
        value = tostring(var.adapter_timeout_seconds)
      }
      dynamic "env" {
        for_each = var.servicenow_caller != null ? [var.servicenow_caller] : []
        content {
          name  = "SERVICENOW_CALLER"
          value = env.value
        }
      }
      env {
        name  = "SERVICENOW_INCIDENT_STATE_AFTER_REPORT"
        value = var.servicenow_incident_state_after_report
      }
      env {
        name  = "GITHUB_APP_ID"
        value = var.github_app_id
      }
      env {
        name = "GITHUB_APP_PRIVATE_KEY"
        value_source {
          secret_key_ref {
            secret  = data.google_secret_manager_secret.github_app_private_key.secret_id
            version = "latest"
          }
        }
      }

      env {
        name = "SERVICENOW_USERNAME"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.servicenow_username.secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "GITHUB_WEBHOOK_SECRET"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.github_webhook_secret.secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "SERVICENOW_PASSWORD"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.servicenow_password.secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "SERVICENOW_WEBHOOK_TOKEN"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.servicenow_webhook_token.secret_id
            version = "latest"
          }
        }
      }
    }
  }

  depends_on = [
    google_secret_manager_secret_iam_member.bridge_gh_secret_access,
    google_secret_manager_secret_iam_member.bridge_snow_username_access,
    google_secret_manager_secret_iam_member.bridge_snow_pwd_access,
    google_secret_manager_secret_iam_member.bridge_snow_token_access,
    google_secret_manager_secret_iam_member.bridge_gh_key_access,
  ]

  lifecycle {
    ignore_changes = [
      client,
      client_version,
    ]
  }
}

# Allow public access for inbound webhooks and callbacks
resource "google_cloud_run_v2_service_iam_member" "public_invoker" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.bridge.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}
