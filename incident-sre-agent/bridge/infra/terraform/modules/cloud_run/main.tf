resource "google_cloud_run_v2_service" "bridge" {
  project  = var.project_id
  location = var.region
  name     = var.service_name
  ingress  = "INGRESS_TRAFFIC_ALL"
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
        name  = "GCP_PROJECT"
        value = var.project_id
      }
      env {
        name  = "GCP_REGION"
        value = var.region
      }
      env {
        name  = "TASKS_QUEUE"
        value = var.tasks_queue_name
      }
      env {
        name  = "TASKS_LOCATION"
        value = var.region
      }
      env {
        name  = "TASKS_INVOKER_SA"
        value = var.tasks_invoker_service_account_email
      }
      env {
        name  = "AGENT_SERVICE_URL"
        value = var.agent_service_url
      }
      env {
        name  = "AGENT_SA"
        value = var.agent_service_account_email
      }
      env {
        name  = "CALLBACK_URL"
        value = var.callback_url
      }
      env {
        name  = "SERVICENOW_INSTANCE_URL"
        value = var.servicenow_instance_url
      }
      env {
        name  = "JIRA_BASE_URL"
        value = var.jira_base_url
      }
      env {
        name = "SERVICENOW_USERNAME"
        value_source {
          secret_key_ref {
            secret  = "${var.name_prefix}-bridge-servicenow-username"
            version = "latest"
          }
        }
      }
      env {
        name = "SERVICENOW_PASSWORD"
        value_source {
          secret_key_ref {
            secret  = "${var.name_prefix}-bridge-servicenow-password"
            version = "latest"
          }
        }
      }
      env {
        name = "SERVICENOW_WEBHOOK_TOKEN"
        value_source {
          secret_key_ref {
            secret  = "${var.name_prefix}-bridge-servicenow-webhook-token"
            version = "latest"
          }
        }
      }
      env {
        name = "JIRA_EMAIL"
        value_source {
          secret_key_ref {
            secret  = "${var.name_prefix}-bridge-jira-email"
            version = "latest"
          }
        }
      }
      env {
        name = "JIRA_API_TOKEN"
        value_source {
          secret_key_ref {
            secret  = "${var.name_prefix}-bridge-jira-api-token"
            version = "latest"
          }
        }
      }
      env {
        name = "JIRA_WEBHOOK_TOKEN"
        value_source {
          secret_key_ref {
            secret  = "${var.name_prefix}-bridge-jira-webhook-token"
            version = "latest"
          }
        }
      }
      env {
        name = "GENERIC_HMAC_KEY"
        value_source {
          secret_key_ref {
            secret  = "${var.name_prefix}-bridge-generic-hmac-key"
            version = "latest"
          }
        }
      }
    }
  }
}

resource "google_cloud_run_v2_service_iam_member" "public_access" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.bridge.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}
