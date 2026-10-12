locals {
  common_labels = {
    app       = "incident-sre-agent"
    component = "agent"
    env       = var.environment
    prefix    = var.name_prefix
  }

  agent_sa_account_id         = substr("${var.name_prefix}-${var.agent_service_account_id}", 0, 30)
  tasks_invoker_sa_account_id = substr("${var.name_prefix}-${var.tasks_invoker_service_account_id}", 0, 30)
}

module "apis" {
  source     = "./modules/apis"
  project_id = var.project_id
}

module "iam" {
  source                           = "./modules/iam"
  project_id                       = var.project_id
  environment                      = var.environment
  agent_service_account_id         = local.agent_sa_account_id
  tasks_invoker_service_account_id = local.tasks_invoker_sa_account_id

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

module "tasks_queue" {
  source       = "./modules/tasks_queue"
  project_id   = var.project_id
  region       = var.region
  queue_name   = "${var.name_prefix}-${var.queue_name}"
  max_attempts = var.queue_max_attempts

  depends_on = [module.apis]
}

module "cloud_run" {
  source                        = "./modules/cloud_run"
  project_id                    = var.project_id
  region                        = var.region
  service_name                  = "${var.name_prefix}-${var.service_name}"
  vertex_location               = var.vertex_location
  timeout_seconds               = var.cloud_run_timeout_seconds
  cpu                           = var.cloud_run_cpu
  memory                        = var.cloud_run_memory
  container_image               = "${var.region}-docker.pkg.dev/${var.project_id}/${module.artifact_registry.repository_id}/${var.name_prefix}-${var.service_name}:${var.image_tag}"
  service_account_email         = module.iam.agent_service_account_email
  invoker_service_account_email = module.iam.tasks_invoker_service_account_email
  ingress                       = "INGRESS_TRAFFIC_INTERNAL_ONLY"
  labels                        = local.common_labels
  environment                   = var.environment
  name_prefix                   = var.name_prefix
  model_dispatcher              = var.model_dispatcher
  model_synthesizer             = var.model_synthesizer
  model_specialist              = var.model_specialist
  allowed_callback_hosts        = var.allowed_callback_hosts

  depends_on = [module.apis, module.iam]
}

