locals {
  common_labels = {
    app    = "ambient-agent-registry-demo"
    env    = var.environment
    prefix = var.name_prefix
  }

  agent_sa_account_id           = trimsuffix(substr("${var.name_prefix}-agent-sa", 0, 30), "-")
  calculator_tool_sa_account_id = trimsuffix(substr("${var.name_prefix}-calc-sa", 0, 30), "-")

  artifact_registry_repo_id = "${var.name_prefix}-docker-repo"
  repo_base_url             = "${var.region}-docker.pkg.dev/${var.project_id}/${local.artifact_registry_repo_id}"

  agent_image = var.agent_image != null ? var.agent_image : "${local.repo_base_url}/${var.name_prefix}-agent-service:latest"
  tool_image  = var.tool_image != null ? var.tool_image : "${local.repo_base_url}/${var.name_prefix}-calculator-service:latest"
}

module "apis" {
  source     = "./modules/apis"
  project_id = var.project_id
}

module "artifact_registry" {
  source        = "./modules/artifact_registry"
  project_id    = var.project_id
  region        = var.region
  repository_id = local.artifact_registry_repo_id
  labels        = local.common_labels

  depends_on = [module.apis]
}

module "iam" {
  source                             = "./modules/iam"
  project_id                         = var.project_id
  agent_service_account_id           = local.agent_sa_account_id
  calculator_tool_service_account_id = local.calculator_tool_sa_account_id

  depends_on = [module.apis]
}

module "cloud_run" {
  source                   = "./modules/cloud_run"
  project_id               = var.project_id
  region                   = var.region
  name_prefix              = var.name_prefix
  labels                   = local.common_labels
  agent_service_name       = "${var.name_prefix}-agent-service"
  calculator_service_name  = "${var.name_prefix}-calculator-service"
  agent_image              = local.agent_image
  tool_image               = local.tool_image
  agent_sa_email           = module.iam.agent_sa_email
  calculator_tool_sa_email = module.iam.calculator_tool_sa_email
  agent_cpu                = var.agent_cpu
  agent_memory             = var.agent_memory
  agent_timeout_seconds    = var.agent_timeout_seconds
  tool_cpu                 = var.tool_cpu
  tool_memory              = var.tool_memory
  tool_timeout_seconds     = var.tool_timeout_seconds
  agent_registry_endpoint  = var.agent_registry_endpoint
  agent_ingress            = var.agent_ingress
  tool_ingress             = var.tool_ingress

  depends_on = [module.apis, module.iam]
}

module "agent_registry" {
  source                 = "./modules/agent_registry"
  project_id             = var.project_id
  region                 = var.region
  service_id             = "${var.name_prefix}-calculator"
  display_name           = "Ambient Precision Calculator Service"
  description            = "Provides verified arithmetic operations (add, multiply, power)"
  calculator_service_url = module.cloud_run.calculator_service_url

  depends_on = [module.apis, module.cloud_run]
}
