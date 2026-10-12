locals {
  common_labels = {
    app       = "incident-sre-agent"
    component = "demo"
    env       = var.environment
    prefix    = var.name_prefix
  }

  shop_api_service_name      = "${var.name_prefix}-${var.shop_api_service_name}"
  inventory_svc_service_name = "${var.name_prefix}-${var.inventory_svc_service_name}"
  shop_api_sa_account_id     = substr("${var.name_prefix}-${var.shop_api_sa_account_id}", 0, 30)

  shop_api_image      = var.shop_api_image != "" ? var.shop_api_image : "${var.region}-docker.pkg.dev/${var.project_id}/${var.name_prefix}-incident-demo/${var.name_prefix}-shop-api:${var.image_tag}"
  inventory_svc_image = var.inventory_svc_image != "" ? var.inventory_svc_image : "${var.region}-docker.pkg.dev/${var.project_id}/${var.name_prefix}-incident-demo/${var.name_prefix}-inventory-svc:${var.image_tag}"
}

# Service account for shop-api
resource "google_service_account" "shop_api_sa" {
  account_id   = local.shop_api_sa_account_id
  display_name = "Shop API Service Account"
  project      = var.project_id
}

# Private Inventory Service (requires authentication)
resource "google_cloud_run_v2_service" "inventory_svc" {
  name     = local.inventory_svc_service_name
  location = var.region
  project  = var.project_id
  ingress  = "INGRESS_TRAFFIC_ALL"
  labels   = local.common_labels

  template {
    timeout = "${var.cloud_run_timeout_seconds}s"

    containers {
      image = local.inventory_svc_image

      resources {
        limits = {
          cpu    = var.cloud_run_cpu
          memory = var.cloud_run_memory
        }
      }

      ports {
        container_port = 8080
      }

      env {
        name  = "REQUIRE_AUTH"
        value = "true"
      }
    }
  }
}

# Grant shop-api-sa invoker permission on inventory-svc
resource "google_cloud_run_v2_service_iam_member" "inventory_invoker" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.inventory_svc.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.shop_api_sa.email}"
}

# Public Storefront Service: shop-api
resource "google_cloud_run_v2_service" "shop_api" {
  name     = local.shop_api_service_name
  location = var.region
  project  = var.project_id
  ingress  = "INGRESS_TRAFFIC_ALL"
  labels   = local.common_labels

  template {
    service_account = google_service_account.shop_api_sa.email
    timeout         = "${var.cloud_run_timeout_seconds}s"

    containers {
      image = local.shop_api_image

      resources {
        limits = {
          cpu    = var.shop_api_cpu
          memory = var.shop_api_memory
        }
      }

      ports {
        container_port = 8080
      }

      env {
        name  = "INVENTORY_SERVICE_URL"
        value = google_cloud_run_v2_service.inventory_svc.uri
      }

      env {
        name  = "PAYMENT_GATEWAY_KEY"
        value = var.payment_gateway_key
      }
    }
  }
}

# Public access to shop-api
resource "google_cloud_run_v2_service_iam_member" "shop_api_public_access" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.shop_api.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

