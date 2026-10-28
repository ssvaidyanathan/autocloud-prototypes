output "incidents_bucket_name" {
  description = "Name of GCS bucket for incident records"
  value       = google_storage_bucket.incidents.name
}

output "incidents_bucket_url" {
  description = "URL of GCS bucket for incident records"
  value       = google_storage_bucket.incidents.url
}

output "triage_queue_name" {
  description = "Cloud Tasks queue name for triage dispatching"
  value       = google_cloud_tasks_queue.triage_queue.name
}

output "triage_queue_id" {
  description = "Cloud Tasks queue resource ID"
  value       = google_cloud_tasks_queue.triage_queue.id
}

output "timer_queue_name" {
  description = "Cloud Tasks queue name for timer dispatching"
  value       = google_cloud_tasks_queue.timer_queue.name
}

output "timer_queue_id" {
  description = "Cloud Tasks queue resource ID"
  value       = google_cloud_tasks_queue.timer_queue.id
}

output "artifact_registry_repo" {
  description = "Artifact Registry Docker repository ID"
  value       = google_artifact_registry_repository.docker_repo.name
}

output "bridge_sa_email" {
  description = "Email of Bridge Service Account"
  value       = google_service_account.bridge_sa.email
}

output "agent_sa_email" {
  description = "Email of Agent Service Account"
  value       = google_service_account.agent_sa.email
}

output "notification_channel_id" {
  description = "Cloud Monitoring notification channel resource name"
  value       = google_monitoring_notification_channel.bridge_webhook.name
}

output "inventory_sa_email" {
  description = "Email of the demo inventory API Service Account"
  value       = google_service_account.inventory_sa.email
}

output "orders_sa_email" {
  description = "Email of the demo orders API Service Account"
  value       = google_service_account.orders_sa.email
}

output "apigee_sa_email" {
  description = "Email of the Apigee proxy Service Account"
  value       = google_service_account.apigee_sa.email
}

output "build_sa_email" {
  description = "Email of the Cloud Build Service Account"
  value       = google_service_account.build_sa.email
}

output "build_sa_id" {
  description = "Full resource name of the build SA, the value of gcloud builds submit --service-account"
  value       = google_service_account.build_sa.id
}

output "build_bucket_name" {
  description = "Bucket for Cloud Build source staging (gs://<bucket>/source, --gcs-source-staging-dir) and logs (gs://<bucket>/logs, --gcs-log-dir)"
  value       = google_storage_bucket.build.name
}

output "project_number" {
  description = "Project number, used in the deterministic Cloud Run URLs https://<service>-<project number>.<region>.run.app"
  value       = data.google_project.this.number
}

output "bridge_webhook_url" {
  description = "Webhook URL of the notification channel (deterministic bridge URL)"
  value       = google_monitoring_notification_channel.bridge_webhook.labels.url
}
