output "bridge_service_account_email" {
  description = "Email of the Incident Bridge service account"
  value       = google_service_account.bridge_sa.email
}

output "bridge_service_account_name" {
  description = "Fully qualified name of the Incident Bridge service account"
  value       = google_service_account.bridge_sa.name
}
