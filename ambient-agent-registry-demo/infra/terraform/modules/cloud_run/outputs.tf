output "agent_service_name" {
  description = "Name of the Agent Cloud Run service"
  value       = google_cloud_run_v2_service.agent.name
}

output "agent_service_url" {
  description = "URL of the Agent Cloud Run service"
  value       = google_cloud_run_v2_service.agent.uri
}

output "calculator_service_name" {
  description = "Name of the Calculator Tool Cloud Run service"
  value       = google_cloud_run_v2_service.calculator.name
}

output "calculator_service_url" {
  description = "URL of the Calculator Tool Cloud Run service"
  value       = google_cloud_run_v2_service.calculator.uri
}
