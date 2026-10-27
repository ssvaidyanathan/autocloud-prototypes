locals {
  common_labels = {
    app        = "alert-triage-agent"
    role       = "triage-system"
    tier       = "platform"
    env        = var.environment
    managed-by = "terraform"
    owner      = var.owner
  }
}

# 1. GCS Bucket for Incidents (OKF Markdown State Store)
resource "google_storage_bucket" "incidents" {
  name                        = "${var.project_id}-${var.name_prefix}-incidents"
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = false

  versioning {
    enabled = true
  }

  labels = merge(local.common_labels, {
    component = "${var.name_prefix}-agent"
  })
}

# 2. Cloud Tasks Queue (Serialized Triage Queue)
# Note: Cloud Tasks queues do not support GCP resource labels.
resource "google_cloud_tasks_queue" "triage_queue" {
  name     = "${var.name_prefix}-triage-queue"
  location = var.region

  rate_limits {
    max_concurrent_dispatches = 1
    max_dispatches_per_second = 10
  }

  # Cloud Tasks stops retrying only once BOTH max_attempts and max_retry_duration
  # are satisfied, so a small max_attempts makes the duration the binding limit.
  retry_config {
    max_attempts       = var.triage_queue_max_attempts
    max_retry_duration = var.triage_queue_max_retry_duration
    min_backoff        = var.triage_queue_min_backoff
    max_backoff        = var.triage_queue_max_backoff
  }
}

resource "google_cloud_tasks_queue" "timer_queue" {
  name     = "${var.name_prefix}-timer-queue"
  location = var.region
  rate_limits {
    max_concurrent_dispatches = 50
    max_dispatches_per_second = 50
  }
}

# 3. Artifact Registry Docker Repository
resource "google_artifact_registry_repository" "docker_repo" {
  repository_id = "${var.name_prefix}-docker"
  location      = var.region
  format        = "DOCKER"
  description   = "Docker repository for alert triage agent services"
  labels = merge(local.common_labels, {
    component = "${var.name_prefix}-tools"
  })
}

# 4. Service Accounts
resource "google_service_account" "bridge_sa" {
  account_id   = "${var.name_prefix}-bridge-sa"
  display_name = "Alert Triage Bridge Service Account"
  description  = "app=alert-triage-agent,component=${var.name_prefix}-bridge,role=triage-system,tier=platform,env=${var.environment},managed-by=terraform,owner=${var.owner}"
}

resource "google_service_account" "agent_sa" {
  account_id   = "${var.name_prefix}-agent-sa"
  display_name = "Alert Triage Agent Service Account"
  description  = "app=alert-triage-agent,component=${var.name_prefix}-agent,role=triage-system,tier=platform,env=${var.environment},managed-by=terraform,owner=${var.owner}"
}

# Bridge SA IAM Roles
resource "google_project_iam_member" "bridge_roles" {
  for_each = toset([
    "roles/cloudtasks.enqueuer",
    "roles/secretmanager.secretAccessor",
    "roles/storage.objectUser",
    "roles/run.invoker"
  ])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.bridge_sa.email}"
}

# Allow Bridge SA to act as service accounts for Cloud Tasks OIDC token generation
resource "google_project_iam_member" "bridge_sa_service_account_user" {
  project = var.project_id
  role    = "roles/iam.serviceAccountUser"
  member  = "serviceAccount:${google_service_account.bridge_sa.email}"
}

# Agent SA Viewer Roles on Production Project (Strictly Read-Only Production Boundary)
resource "google_project_iam_member" "agent_viewer_roles" {
  for_each = toset([
    "roles/logging.viewer",
    "roles/monitoring.viewer",
    "roles/run.viewer",
    "roles/cloudsql.viewer"
  ])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.agent_sa.email}"
}

# Agent SA Write Access strictly scoped to Incidents Bucket Only
resource "google_storage_bucket_iam_member" "agent_incidents_writer" {
  bucket = google_storage_bucket.incidents.name
  role   = "roles/storage.objectUser"
  member = "serviceAccount:${google_service_account.agent_sa.email}"
}

# Allow Agent SA to enqueue timer tasks and act as service account for Cloud Tasks OIDC token generation
resource "google_project_iam_member" "agent_cloudtasks_enqueuer" {
  project = var.project_id
  role    = "roles/cloudtasks.enqueuer"
  member  = "serviceAccount:${google_service_account.agent_sa.email}"
}

resource "google_project_iam_member" "agent_sa_service_account_user" {
  project = var.project_id
  role    = "roles/iam.serviceAccountUser"
  member  = "serviceAccount:${google_service_account.agent_sa.email}"
}

# 5. Webhook Token Generated Directly in Terraform (Sensitive, never user-entered)
resource "random_password" "webhook_token" {
  length  = 32
  special = false
  keepers = {
    rotation = "v2_basicauth"
  }
}

resource "google_secret_manager_secret" "webhook_token" {
  secret_id = "${var.name_prefix}-webhook-token"

  replication {
    auto {}
  }

  labels = merge(local.common_labels, {
    component = "${var.name_prefix}-bridge"
  })
}

resource "google_secret_manager_secret_version" "webhook_token_version" {
  secret      = google_secret_manager_secret.webhook_token.id
  secret_data = random_password.webhook_token.result
}

# 6. ServiceNow Secret Placeholders (User sets ONLY these 2 credentials via set-secrets.sh)
locals {
  servicenow_secret_keys = [
    "servicenow-username",
    "servicenow-password"
  ]
}

resource "google_secret_manager_secret" "servicenow_placeholders" {
  for_each  = toset(local.servicenow_secret_keys)
  secret_id = "${var.name_prefix}-${each.value}"

  replication {
    auto {}
  }

  labels = merge(local.common_labels, {
    component = "${var.name_prefix}-bridge"
  })
}

resource "google_secret_manager_secret_version" "servicenow_placeholder_versions" {
  for_each    = toset(local.servicenow_secret_keys)
  secret      = google_secret_manager_secret.servicenow_placeholders[each.key].id
  secret_data = "placeholder"

  lifecycle {
    ignore_changes = [secret_data]
  }
}

# 7. Cloud Monitoring Webhook Notification Channel
resource "google_monitoring_notification_channel" "bridge_webhook" {
  project      = var.project_id
  display_name = "${var.name_prefix}-bridge-webhook"
  type         = "webhook_basicauth"
  labels = {
    url      = "https://${var.name_prefix}-bridge-4klvlqnmpa-uw.a.run.app/webhook/monitoring"
    username = "ata-monitoring"
  }
  sensitive_labels {
    password = random_password.webhook_token.result
  }
  user_labels = merge(local.common_labels, {
    component = "${var.name_prefix}-bridge"
  })
}
