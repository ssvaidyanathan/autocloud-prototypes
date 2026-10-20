output "service_id" {
  description = "Service ID of the registered MCP service"
  value       = google_agent_registry_service.calculator.service_id
}

output "registry_resource" {
  description = "Full resource name of the registered MCP service"
  value       = "projects/${var.project_id}/locations/${var.region}/services/${var.service_id}"
}
