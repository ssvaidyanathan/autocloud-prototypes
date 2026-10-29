resource "google_service_account" "bridge_sa" {
  project      = var.project_id
  account_id   = var.bridge_service_account_id
  display_name = "Incident Bridge Service Account"
  description  = "Service account for Incident Bridge with Cloud Tasks enqueue and Secret Manager access"
}

resource "google_cloud_tasks_queue_iam_member" "enqueuer" {
  project  = var.project_id
  location = var.region
  name     = var.tasks_queue_name
  role     = "roles/cloudtasks.enqueuer"
  member   = "serviceAccount:${google_service_account.bridge_sa.email}"
}

resource "google_service_account_iam_member" "tasks_invoker_user" {
  service_account_id = "projects/${var.project_id}/serviceAccounts/${var.tasks_invoker_service_account_email}"
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.bridge_sa.email}"
}
