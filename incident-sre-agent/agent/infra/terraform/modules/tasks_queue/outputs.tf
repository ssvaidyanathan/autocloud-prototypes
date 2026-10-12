output "queue_id" {
  description = "The Cloud Tasks queue ID"
  value       = google_cloud_tasks_queue.investigations.id
}

output "queue_name" {
  description = "The Cloud Tasks queue name"
  value       = google_cloud_tasks_queue.investigations.name
}
