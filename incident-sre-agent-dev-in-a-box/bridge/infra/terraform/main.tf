locals {
  common_labels = {
    app       = "incident-sre-agent"
    component = "bridge"
    env       = var.environment
    prefix    = var.name_prefix
  }

  bridge_sa_account_id       = substr("${var.name_prefix}-${var.bridge_service_account_id}", 0, 30)
  effective_tasks_queue_name = startswith(var.tasks_queue_name, "${var.name_prefix}-") ? var.tasks_queue_name : "${var.name_prefix}-${var.tasks_queue_name}"
}

module "apis" {
  source     = "./modules/apis"
  project_id = var.project_id
}

module "iam" {
  source                              = "./modules/iam"
  project_id                          = var.project_id
  region                              = var.region
  tasks_queue_name                    = local.effective_tasks_queue_name
  tasks_invoker_service_account_email = var.tasks_invoker_service_account_email
  bridge_service_account_id           = local.bridge_sa_account_id

  depends_on = [module.apis]
}

module "artifact_registry" {
  source        = "./modules/artifact_registry"
  project_id    = var.project_id
  region        = var.region
  repository_id = "${var.name_prefix}-${var.repository_id}"
  labels        = local.common_labels

  depends_on = [module.apis]
}

module "secrets" {
  source                       = "./modules/secrets"
  project_id                   = var.project_id
  name_prefix                  = var.name_prefix
  bridge_service_account_email = module.iam.bridge_service_account_email
  labels                       = local.common_labels

  depends_on = [module.apis, module.iam]
}

module "cloud_run" {
  source                              = "./modules/cloud_run"
  project_id                          = var.project_id
  region                              = var.region
  service_name                        = "${var.name_prefix}-${var.service_name}"
  container_image                     = "${var.region}-docker.pkg.dev/${var.project_id}/${module.artifact_registry.repository_id}/${var.name_prefix}-${var.service_name}:${var.image_tag}"
  service_account_email               = module.iam.bridge_service_account_email
  tasks_queue_name                    = local.effective_tasks_queue_name
  tasks_invoker_service_account_email = var.tasks_invoker_service_account_email
  agent_service_url                   = var.agent_service_url
  agent_service_account_email         = var.agent_service_account_email
  callback_url                        = var.callback_url
  timeout_seconds                     = var.cloud_run_timeout_seconds
  cpu                                 = var.cloud_run_cpu
  memory                              = var.cloud_run_memory
  labels                              = local.common_labels
  name_prefix                         = var.name_prefix
  environment                         = var.environment
  servicenow_instance_url             = var.servicenow_instance_url
  jira_base_url                       = var.jira_base_url

  depends_on = [module.apis, module.iam, module.secrets]
}

