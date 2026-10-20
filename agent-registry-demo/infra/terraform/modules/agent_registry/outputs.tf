output "service_id" {
  description = "The service ID in Agent Registry"
  value       = google_agent_registry_service.calculator.service_id
}

output "service_name" {
  description = "The full resource name of the Agent Registry Service"
  value       = google_agent_registry_service.calculator.name
}

output "registry_resource" {
  description = "The resource name of the resulting MCP Server in Tools Registry"
  value       = google_agent_registry_service.calculator.registry_resource
}
