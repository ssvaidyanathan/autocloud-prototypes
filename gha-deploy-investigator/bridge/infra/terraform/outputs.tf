output "bridge_url" {
  description = "The public URL of the GDI Bridge Cloud Run service"
  value       = google_cloud_run_v2_service.bridge.uri
}

output "bridge_service_account_email" {
  description = "The service account email of GDI Bridge"
  value       = google_service_account.bridge_sa.email
}

output "tasks_queue_name" {
  description = "Name of the Cloud Tasks queue"
  value       = google_cloud_tasks_queue.tasks_queue.name
}

output "tasks_queue_id" {
  description = "Full resource ID of the Cloud Tasks queue"
  value       = google_cloud_tasks_queue.tasks_queue.id
}
