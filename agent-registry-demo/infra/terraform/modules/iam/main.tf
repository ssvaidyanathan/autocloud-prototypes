resource "google_service_account" "agent_sa" {
  project      = var.project_id
  account_id   = var.agent_service_account_id
  display_name = "Agent Registry Demo Agent Service Account"
  description  = "Service account for Agent Registry Demo agent service"
}

resource "google_service_account" "calculator_tool_sa" {
  project      = var.project_id
  account_id   = var.calculator_tool_service_account_id
  display_name = "Agent Registry Demo Calculator Tool Service Account"
  description  = "Least privilege service account for Calculator Tool service"
}

locals {
  agent_roles = [
    "roles/aiplatform.user",
    "roles/logging.logWriter",
    "roles/agentregistry.viewer",
  ]

  calculator_roles = [
    "roles/logging.logWriter",
  ]
}

resource "google_project_iam_member" "agent_roles" {
  for_each = toset(local.agent_roles)

  project = var.project_id
  role    = each.key
  member  = "serviceAccount:${google_service_account.agent_sa.email}"
}

resource "google_project_iam_member" "calculator_roles" {
  for_each = toset(local.calculator_roles)

  project = var.project_id
  role    = each.key
  member  = "serviceAccount:${google_service_account.calculator_tool_sa.email}"
}
