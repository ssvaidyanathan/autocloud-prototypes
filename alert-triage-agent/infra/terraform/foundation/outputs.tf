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
