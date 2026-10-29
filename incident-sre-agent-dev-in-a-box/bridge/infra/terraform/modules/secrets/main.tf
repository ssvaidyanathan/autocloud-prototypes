locals {
  base_secret_ids = [
    "bridge-servicenow-username",
    "bridge-servicenow-password",
    "bridge-servicenow-webhook-token",
    "bridge-jira-email",
    "bridge-jira-api-token",
    "bridge-jira-webhook-token",
    "bridge-generic-hmac-key",
  ]
  secret_ids = [for s in local.base_secret_ids : "${var.name_prefix}-${s}"]
}

resource "google_secret_manager_secret" "secrets" {
  for_each  = toset(local.secret_ids)
  project   = var.project_id
  secret_id = each.key
  labels    = var.labels

  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_iam_member" "bridge_sa_accessor" {
  for_each  = google_secret_manager_secret.secrets
  project   = var.project_id
  secret_id = each.value.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.bridge_service_account_email}"
}
