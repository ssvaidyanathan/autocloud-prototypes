output "agent_service_url" {
  description = "URL of the Agent Cloud Run service"
  value       = module.cloud_run.agent_service_url
}

output "calculator_tool_service_url" {
  description = "URL of the Calculator Tool Cloud Run service"
  value       = module.cloud_run.calculator_service_url
}

output "artifact_registry_repository" {
  description = "ID of the Artifact Registry Docker repository"
  value       = module.artifact_registry.repository_id
}

output "agent_sa_email" {
  description = "Email of the Agent Service Account"
  value       = module.iam.agent_sa_email
}

output "calculator_tool_sa_email" {
  description = "Email of the Calculator Tool Service Account"
  value       = module.iam.calculator_tool_sa_email
}

output "agent_registry_service_id" {
  description = "Service ID of the Calculator MCP service in Agent Registry"
  value       = module.agent_registry.service_id
}

output "agent_registry_resource" {
  description = "Full resource name of the MCP Server projected in Tools Registry"
  value       = module.agent_registry.registry_resource
}
