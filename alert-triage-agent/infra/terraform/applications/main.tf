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

locals {
  # Cloud SQL IAM database user for ata-inventory-api. Rendered into both
  # ATA_INVENTORY_DB_USER and its legacy alias DB_USER so the two cannot diverge.
  inventory_db_user = "${var.name_prefix}-inventory-sa@${var.project_id}.iam"
}

# -------------------------------------------------------------
# Cloud Run: ata-inventory-api
# -------------------------------------------------------------
resource "google_cloud_run_v2_service" "inventory_api" {
  name     = "${var.name_prefix}-inventory-api"
  location = var.region
  project  = var.project_id
  ingress  = "INGRESS_TRAFFIC_ALL"

  labels = {
    app        = "alert-triage-agent"
    component  = "${var.name_prefix}-inventory-api"
    role       = "demo-workload"
    tier       = "service"
    env        = var.environment
    managed-by = "terraform"
    owner      = var.owner
  }

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
        value = local.inventory_db_user
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
        value = tostring(var.inventory_mock_db)
      }
      # Legacy aliases. The app reads ATA_INVENTORY_* first and falls back to
      # these (demo/ata-inventory-api/app/config.py). deploy.sh, s5_bad_deploy.sh
      # and reset-scenarios.sh set both name families, so they are declared here
      # to keep Terraform in line with the live service.
      env {
        name  = "DB_USER"
        value = local.inventory_db_user
      }
      env {
        name  = "MOCK_DB"
        value = tostring(var.inventory_mock_db)
      }
    }
  }

  lifecycle {
    # client/client_version record which tool last wrote the service (gcloud
    # stamps "gcloud"/<sdk version>). They are not configuration and do not
    # affect revisions; the demo scripts update this service via gcloud.
    ignore_changes = [client, client_version]
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

  labels = {
    app        = "alert-triage-agent"
    component  = "${var.name_prefix}-orders-api"
    role       = "demo-workload"
    tier       = "service"
    env        = var.environment
    managed-by = "terraform"
    owner      = var.owner
  }

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

  lifecycle {
    # See inventory_api: tool-of-last-write bookkeeping, not configuration.
    ignore_changes = [client, client_version]
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
