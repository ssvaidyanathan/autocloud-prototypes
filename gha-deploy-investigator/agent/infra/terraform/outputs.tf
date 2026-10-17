output "agent_url" {
  description = "The internal URL of the GDI Agent Cloud Run service"
  value       = google_cloud_run_v2_service.agent.uri
}

output "agent_service_account_email" {
  description = "The service account email of GDI Agent"
  value       = google_service_account.agent_sa.email
}

output "tasks_invoker_service_account_email" {
  description = "The service account email for Cloud Tasks invocation"
  value       = google_service_account.tasks_invoker_sa.email
}
