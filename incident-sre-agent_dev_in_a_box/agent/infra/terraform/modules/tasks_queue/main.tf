resource "google_cloud_tasks_queue" "investigations" {
  project  = var.project_id
  location = var.region
  name     = var.queue_name

  retry_config {
    max_attempts       = var.max_attempts
    max_retry_duration = "3600s"
  }
}
