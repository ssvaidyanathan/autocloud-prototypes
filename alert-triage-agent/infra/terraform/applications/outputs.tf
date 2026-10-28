output "inventory_api_uri" {
  description = "URI of <name_prefix>-inventory-api service"
  value       = google_cloud_run_v2_service.inventory_api.uri
}

output "inventory_api_name" {
  description = "Name of <name_prefix>-inventory-api service"
  value       = google_cloud_run_v2_service.inventory_api.name
}

output "orders_api_uri" {
  description = "URI of <name_prefix>-orders-api service"
  value       = google_cloud_run_v2_service.orders_api.uri
}

output "orders_api_name" {
  description = "Name of <name_prefix>-orders-api service"
  value       = google_cloud_run_v2_service.orders_api.name
}
