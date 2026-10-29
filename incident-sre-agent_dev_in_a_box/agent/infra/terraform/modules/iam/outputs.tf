output "agent_service_account_email" {
  description = "Email of the SRE Agent service account"
  value       = google_service_account.agent_sa.email
}

output "agent_service_account_id" {
  description = "ID of the SRE Agent service account"
  value       = google_service_account.agent_sa.id
}

output "tasks_invoker_service_account_email" {
  description = "Email of the Cloud Tasks invoker service account"
  value       = google_service_account.tasks_invoker_sa.email
}

output "tasks_invoker_service_account_id" {
  description = "ID of the Cloud Tasks invoker service account"
  value       = google_service_account.tasks_invoker_sa.id
}
