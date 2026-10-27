# Runtime SAs are created by the foundation root. Their emails are deterministic,
# so they are derived here instead of read back (no data lookup, no dependency on
# the foundation state location). Foundation must be applied first; if it was
# not, the Cloud Run apply fails loudly on the missing SA.
locals {
  inventory_sa_email = "${var.name_prefix}-inventory-sa@${var.project_id}.iam.gserviceaccount.com"
  orders_sa_email    = "${var.name_prefix}-orders-sa@${var.project_id}.iam.gserviceaccount.com"
  apigee_sa_email    = "${var.name_prefix}-apigee-sa@${var.project_id}.iam.gserviceaccount.com"
}

locals {
  # Cloud SQL IAM database user for ata-inventory-api (ATA_INVENTORY_DB_USER).
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
  # Provider 6.x defaults deletion_protection to true, which makes `terraform destroy`
  # (scripts/teardown-stack.sh) fail. The stack is removed only through the teardown.
  deletion_protection = false

  labels = {
    app        = "alert-triage-agent"
    stack      = var.name_prefix
    component  = "${var.name_prefix}-inventory-api"
    role       = "demo-workload"
    tier       = "service"
    env        = var.environment
    managed-by = "terraform"
    owner      = var.owner
  }

  template {
    service_account = local.inventory_sa_email

    # One instance holds the fault state and leaked connections; see var.demo_max_instance_count.
    # Min 0: scale to zero (min instances 1 is not approved for the demo).
    scaling {
      min_instance_count = 0
      max_instance_count = var.demo_max_instance_count
    }

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
      # Only ATA_INVENTORY_* names: the app no longer reads the legacy DB_USER /
      # MOCK_DB aliases (demo/ata-inventory-api/app/config.py), and deploy.sh,
      # s5_bad_deploy.sh and the reset restore no longer set them.
    }
  }

  lifecycle {
    # client/client_version record which tool last wrote the service (gcloud
    # stamps "gcloud"/<sdk version>). They are not configuration and do not
    # affect revisions; the demo scripts update this service via gcloud.
    # scaling is the service-level block (not template.scaling), which this
    # config does not manage; provider 6.x reports its zero defaults as drift.
    ignore_changes = [client, client_version, scaling]
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
  # Provider 6.x defaults deletion_protection to true, which makes `terraform destroy`
  # (scripts/teardown-stack.sh) fail. The stack is removed only through the teardown.
  deletion_protection = false

  labels = {
    app        = "alert-triage-agent"
    stack      = var.name_prefix
    component  = "${var.name_prefix}-orders-api"
    role       = "demo-workload"
    tier       = "service"
    env        = var.environment
    managed-by = "terraform"
    owner      = var.owner
  }

  template {
    service_account = local.orders_sa_email

    scaling {
      min_instance_count = 0
      max_instance_count = var.demo_max_instance_count
    }

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
    # See inventory_api: tool-of-last-write bookkeeping and the unmanaged
    # service-level scaling block, not configuration.
    ignore_changes = [client, client_version, scaling]
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
  member   = "serviceAccount:${local.orders_sa_email}"
}

resource "google_cloud_run_v2_service_iam_member" "inventory_invoker_apigee" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.inventory_api.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${local.apigee_sa_email}"
}

resource "google_cloud_run_v2_service_iam_member" "orders_invoker_apigee" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.orders_api.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${local.apigee_sa_email}"
}

# The triage-tier invoker bindings (bridge SA and agent SA on the agent, agent SA
# on the bridge and tools) moved to the triage-iam root, which is applied after
# the triage services are deployed (they do not exist yet when this root is
# applied). The removed blocks drop them from this root's state WITHOUT
# destroying the live bindings; in a fresh project they are no-ops.
removed {
  from = google_cloud_run_v2_service_iam_member.agent_invoker_bridge
  lifecycle {
    destroy = false
  }
}

removed {
  from = google_cloud_run_v2_service_iam_member.agent_invoker_agent
  lifecycle {
    destroy = false
  }
}

removed {
  from = google_cloud_run_v2_service_iam_member.bridge_invoker_agent
  lifecycle {
    destroy = false
  }
}
