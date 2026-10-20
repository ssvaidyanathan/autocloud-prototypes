resource "google_cloud_run_v2_service" "calculator" {
  project  = var.project_id
  location = var.region
  name     = var.calculator_service_name
  ingress  = var.ingress
  labels   = var.labels

  template {
    service_account = var.calculator_tool_sa_email
    timeout         = "${var.tool_timeout_seconds}s"

    containers {
      image = var.tool_image

      resources {
        limits = {
          cpu    = var.tool_cpu
          memory = var.tool_memory
        }
      }

      ports {
        container_port = 8080
      }

      env {
        name  = "PORT"
        value = "8080"
      }
      env {
        name  = "LOG_LEVEL"
        value = "INFO"
      }

      startup_probe {
        http_get {
          path = "/health"
          port = 8080
        }
        initial_delay_seconds = 5
        period_seconds        = 10
        failure_threshold     = 3
      }

      liveness_probe {
        http_get {
          path = "/health"
          port = 8080
        }
        period_seconds    = 15
        failure_threshold = 3
      }
    }
  }
}

resource "google_cloud_run_v2_service" "agent" {
  project  = var.project_id
  location = var.region
  name     = var.agent_service_name
  ingress  = var.ingress
  labels   = var.labels

  template {
    service_account = var.agent_sa_email
    timeout         = "${var.agent_timeout_seconds}s"

    containers {
      image = var.agent_image

      resources {
        limits = {
          cpu    = var.agent_cpu
          memory = var.agent_memory
        }
      }

      ports {
        container_port = 8080
      }

      env {
        name  = "PROJECT_ID"
        value = var.project_id
      }
      env {
        name  = "REGION"
        value = var.region
      }
      env {
        name  = "AGENT_REGISTRY_ENDPOINT"
        value = var.agent_registry_endpoint
      }
      env {
        name  = "TOOL_SERVICE_URL"
        value = google_cloud_run_v2_service.calculator.uri
      }
      env {
        name  = "PORT"
        value = "8080"
      }
      env {
        name  = "LOG_LEVEL"
        value = "INFO"
      }
      env {
        name  = "SERVICE_NAME"
        value = var.agent_service_name
      }

      startup_probe {
        http_get {
          path = "/health"
          port = 8080
        }
        initial_delay_seconds = 5
        period_seconds        = 10
        failure_threshold     = 3
      }

      liveness_probe {
        http_get {
          path = "/health"
          port = 8080
        }
        period_seconds    = 15
        failure_threshold = 3
      }
    }
  }
}

resource "google_cloud_run_v2_service_iam_member" "agent_invokes_calculator" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.calculator.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${var.agent_sa_email}"
}
