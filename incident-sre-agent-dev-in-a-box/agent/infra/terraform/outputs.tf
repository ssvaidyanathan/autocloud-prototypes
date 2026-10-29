output "agent_service_name" {
  description = "Name of the private SRE Agent Cloud Run service"
  value       = module.cloud_run.service_name
}

output "agent_service_url" {
  description = "URL of the private SRE Agent Cloud Run service"
  value       = module.cloud_run.service_uri
}

output "agent_service_account_email" {
  description = "Email of the SRE Agent service account"
  value       = module.iam.agent_service_account_email
}

output "tasks_invoker_service_account_email" {
  description = "Email of the Cloud Tasks invoker service account"
  value       = module.iam.tasks_invoker_service_account_email
}

output "tasks_queue_name" {
  description = "Name of the investigations Cloud Tasks queue"
  value       = module.tasks_queue.queue_name
}

output "artifact_registry_repo" {
  description = "Name/ID of the Artifact Registry Docker repository"
  value       = module.artifact_registry.repository_id
}
