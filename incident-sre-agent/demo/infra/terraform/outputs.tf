output "shop_api_service_name" {
  description = "Name of the shop-api Cloud Run service"
  value       = google_cloud_run_v2_service.shop_api.name
}

output "inventory_svc_service_name" {
  description = "Name of the inventory-svc Cloud Run service"
  value       = google_cloud_run_v2_service.inventory_svc.name
}

output "shop_api_url" {
  description = "Public URL for shop-api"
  value       = google_cloud_run_v2_service.shop_api.uri
}

output "inventory_svc_url" {
  description = "Private URL for inventory-svc"
  value       = google_cloud_run_v2_service.inventory_svc.uri
}

output "shop_api_sa_email" {
  description = "Service account email for shop-api"
  value       = google_service_account.shop_api_sa.email
}
