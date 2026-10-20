output "agent_sa_email" {
  description = "Email of the Agent Service Account"
  value       = google_service_account.agent_sa.email
}

output "agent_sa_id" {
  description = "Resource ID of the Agent Service Account"
  value       = google_service_account.agent_sa.id
}

output "calculator_tool_sa_email" {
  description = "Email of the Calculator Tool Service Account"
  value       = google_service_account.calculator_tool_sa.email
}

output "calculator_tool_sa_id" {
  description = "Resource ID of the Calculator Tool Service Account"
  value       = google_service_account.calculator_tool_sa.id
}
