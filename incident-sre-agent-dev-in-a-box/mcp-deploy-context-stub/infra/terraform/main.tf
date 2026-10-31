resource "google_service_account" "mcp_stub_sa" {
  project      = var.project_id
  account_id   = "mcp-deploy-stub-sa"
  display_name = "MCP Deployment Context Stub Service Account"
  description  = "Dedicated runtime identity for standalone mcp-deploy-context-stub"
}

resource "google_cloud_run_service" "mcp_service" {
  name     = var.service_name
  location = var.region
  project  = var.project_id

  metadata {
    annotations = {
      "run.googleapis.com/ingress" = var.ingress
    }
  }

  template {
    spec {
      service_account_name = google_service_account.mcp_stub_sa.email

      containers {
        image = var.image

        ports {
          container_port = 8080
        }

        env {
          name  = "PORT"
          value = "8080"
        }

        env {
          name  = "AUTH_DISABLED"
          value = tostring(var.auth_disabled)
        }

        env {
          name  = "AUTHORIZED_CALLER_SERVICE_ACCOUNTS"
          value = join(",", var.authorized_caller_service_accounts)
        }

        env {
          name  = "FIXTURES_GCS_URI"
          value = var.fixtures_gcs_uri
        }

        resources {
          limits = {
            cpu    = "1000m"
            memory = "512Mi"
          }
        }

        startup_probe {
          http_get {
            path = "/health"
            port = 8080
          }
          initial_delay_seconds = 2
          period_seconds        = 5
          failure_threshold     = 3
        }

        liveness_probe {
          http_get {
            path = "/health"
            port = 8080
          }
          period_seconds    = 10
          failure_threshold = 3
        }
      }
    }
  }

  traffic {
    percent         = 100
    latest_revision = true
  }
}

resource "google_cloud_run_service_iam_member" "invokers" {
  for_each = toset(var.authorized_caller_service_accounts)

  project  = var.project_id
  service  = google_cloud_run_service.mcp_service.name
  location = google_cloud_run_service.mcp_service.location
  role     = "roles/run.invoker"
  member   = "serviceAccount:${each.value}"
}
