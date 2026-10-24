data "google_service_account" "inventory_sa" {
  account_id = "${var.name_prefix}-inventory-sa"
  project    = var.project_id
}

data "google_service_account" "orders_sa" {
  account_id = "${var.name_prefix}-orders-sa"
  project    = var.project_id
}

data "google_service_account" "apigee_sa" {
  account_id = "${var.name_prefix}-apigee-sa"
  project    = var.project_id
}

# -------------------------------------------------------------
# Cloud Run: ata-inventory-api
# -------------------------------------------------------------
resource "google_cloud_run_v2_service" "inventory_api" {
  name     = "${var.name_prefix}-inventory-api"
  location = var.region
  project  = var.project_id
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = data.google_service_account.inventory_sa.email

    containers {
      image = var.inventory_image

      resources {
        limits = {
          cpu    = "1000m"
          memory = "512Mi"
        }
      }

      env {
        name  = "ATA_INVENTORY_INSTANCE_CONNECTION_NAME"
        value = "${var.project_id}:${var.region}:${var.name_prefix}-inventory-db"
      }
      env {
        name  = "ATA_INVENTORY_DB_USER"
        value = "${var.name_prefix}-inventory-sa@${var.project_id}.iam"
      }
      env {
        name  = "ATA_INVENTORY_DB_NAME"
        value = "inventory"
      }
      env {
        name  = "ATA_INVENTORY_DB_IAM_AUTH"
        value = "true"
      }
      env {
        name  = "ATA_INVENTORY_MOCK_DB"
        value = "false"
      }
    }
  }
}

# -------------------------------------------------------------
# Cloud Run: ata-orders-api
# -------------------------------------------------------------
resource "google_cloud_run_v2_service" "orders_api" {
  name     = "${var.name_prefix}-orders-api"
  location = var.region
  project  = var.project_id
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = data.google_service_account.orders_sa.email

    containers {
      image = var.orders_image

      resources {
        limits = {
          cpu    = "1000m"
          memory = "512Mi"
        }
      }

      env {
        name  = "ATA_ORDERS_INVENTORY_SERVICE_URL"
        value = google_cloud_run_v2_service.inventory_api.uri
      }
    }
  }
}

# -------------------------------------------------------------
# IAM Invoker Permissions
# -------------------------------------------------------------
resource "google_cloud_run_v2_service_iam_member" "inventory_invoker_orders" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.inventory_api.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${data.google_service_account.orders_sa.email}"
}

resource "google_cloud_run_v2_service_iam_member" "inventory_invoker_apigee" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.inventory_api.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${data.google_service_account.apigee_sa.email}"
}

resource "google_cloud_run_v2_service_iam_member" "orders_invoker_apigee" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.orders_api.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${data.google_service_account.apigee_sa.email}"
}

data "google_service_account" "bridge_sa" {
  account_id = "${var.name_prefix}-bridge-sa"
  project    = var.project_id
}

data "google_service_account" "agent_sa" {
  account_id = "${var.name_prefix}-agent-sa"
  project    = var.project_id
}

resource "google_cloud_run_v2_service_iam_member" "agent_invoker_bridge" {
  project  = var.project_id
  location = var.region
  name     = "${var.name_prefix}-agent"
  role     = "roles/run.invoker"
  member   = "serviceAccount:${data.google_service_account.bridge_sa.email}"
}

resource "google_cloud_run_v2_service_iam_member" "agent_invoker_agent" {
  project  = var.project_id
  location = var.region
  name     = "${var.name_prefix}-agent"
  role     = "roles/run.invoker"
  member   = "serviceAccount:${data.google_service_account.agent_sa.email}"
}

resource "google_cloud_run_v2_service_iam_member" "bridge_invoker_agent" {
  project  = var.project_id
  location = var.region
  name     = "${var.name_prefix}-bridge"
  role     = "roles/run.invoker"
  member   = "serviceAccount:${data.google_service_account.agent_sa.email}"
}
