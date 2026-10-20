output "enabled_services" {
  description = "List of enabled API services"
  value       = [for s in google_project_service.apis : s.service]
}
