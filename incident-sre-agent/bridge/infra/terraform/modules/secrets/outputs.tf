output "secret_ids" {
  description = "List of created Secret Manager secret IDs"
  value       = local.secret_ids
}

output "secrets" {
  description = "Map of created secret resources"
  value       = { for k, v in google_secret_manager_secret.secrets : k => v.id }
}
