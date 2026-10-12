resource "google_cloud_run_v2_service" "agent" {
  project  = var.project_id
  location = var.region
  name     = var.service_name
  ingress  = var.ingress
  labels   = var.labels

  template {
    service_account = var.service_account_email
    timeout         = "${var.timeout_seconds}s"

    containers {
      image = var.container_image

      resources {
        limits = {
          cpu    = var.cpu
          memory = var.memory
        }
      }

      ports {
        container_port = 8080
      }

      env {
        name  = "APP_ENV"
        value = var.environment
      }
      env {
        name  = "NAME_PREFIX"
        value = var.name_prefix
      }
      env {
        name  = "MODEL_DISPATCHER"
        value = var.model_dispatcher
      }
      env {
        name  = "MODEL_SYNTHESIZER"
        value = var.model_synthesizer
      }
      env {
        name  = "MODEL_SPECIALIST"
        value = var.model_specialist
      }
      env {
        name  = "ALLOWED_CALLBACK_HOSTS"
        value = var.allowed_callback_hosts
      }
      env {
        name  = "GCP_PROJECT"
        value = var.project_id
      }
      env {
        name  = "GCP_REGION"
        value = var.region
      }
      env {
        name  = "VERTEX_LOCATION"
        value = var.vertex_location
      }
    }
  }
}

resource "google_cloud_run_v2_service_iam_member" "invoker" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.agent.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${var.invoker_service_account_email}"
}
