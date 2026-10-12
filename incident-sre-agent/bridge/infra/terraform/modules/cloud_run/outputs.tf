output "service_uri" {
  description = "The URI of the Incident Bridge Cloud Run service"
  value       = google_cloud_run_v2_service.bridge.uri
}

output "service_name" {
  description = "The name of the Incident Bridge Cloud Run service"
  value       = google_cloud_run_v2_service.bridge.name
}
