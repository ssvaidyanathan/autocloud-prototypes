output "service_url" {
  description = "URL of deployed MCP deployment context stub service"
  value       = google_cloud_run_service.mcp_service.status[0].url
}

output "service_name" {
  description = "Name of the Cloud Run service"
  value       = google_cloud_run_service.mcp_service.name
}

output "service_account_email" {
  description = "Service account email running the MCP service"
  value       = google_service_account.mcp_stub_sa.email
}
