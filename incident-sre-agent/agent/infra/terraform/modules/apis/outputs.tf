output "enabled_services" {
  description = "List of enabled GCP services"
  value       = [for s in google_project_service.apis : s.service]
}
