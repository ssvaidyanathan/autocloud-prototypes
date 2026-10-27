# Service-level run.invoker bindings of the triage tier. This root is applied
# AFTER the agent, bridge and tools Cloud Run services are deployed by their
# deploy scripts (it fails with "service not found" before that). The SAs come
# from the foundation root; their emails are deterministic.
#
# The bridge's public invoker (allUsers, for the Cloud Monitoring webhook) is set
# by bridge/scripts/deploy.sh (--allow-unauthenticated) and is not managed here.
locals {
  agent_service  = "${var.name_prefix}-agent"
  bridge_service = "${var.name_prefix}-bridge"
  tools_service  = "${var.name_prefix}-tools"

  agent_sa_member  = "serviceAccount:${var.name_prefix}-agent-sa@${var.project_id}.iam.gserviceaccount.com"
  bridge_sa_member = "serviceAccount:${var.name_prefix}-bridge-sa@${var.project_id}.iam.gserviceaccount.com"
}

# Bridge -> agent: Cloud Tasks triage dispatch (OIDC token as the bridge SA).
resource "google_cloud_run_v2_service_iam_member" "agent_invoker_bridge" {
  project  = var.project_id
  location = var.region
  name     = local.agent_service
  role     = "roles/run.invoker"
  member   = local.bridge_sa_member
}

# Agent -> agent: timer wake tasks (OIDC token as the agent SA).
resource "google_cloud_run_v2_service_iam_member" "agent_invoker_agent" {
  project  = var.project_id
  location = var.region
  name     = local.agent_service
  role     = "roles/run.invoker"
  member   = local.agent_sa_member
}

# Agent -> bridge: /instructions/* ticket calls with an ID token.
resource "google_cloud_run_v2_service_iam_member" "bridge_invoker_agent" {
  project  = var.project_id
  location = var.region
  name     = local.bridge_service
  role     = "roles/run.invoker"
  member   = local.agent_sa_member
}

# Agent -> tools: MCP/HTTP calls to the tools service with an ID token.
resource "google_cloud_run_v2_service_iam_member" "tools_invoker_agent" {
  project  = var.project_id
  location = var.region
  name     = local.tools_service
  role     = "roles/run.invoker"
  member   = local.agent_sa_member
}
